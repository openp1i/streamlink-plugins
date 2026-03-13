"""
$description Global live-streaming and video hosting social platform owned by Google.
$url youtube.com
$url youtu.be
$type live
$metadata id
$metadata author
$metadata category
$metadata title
$notes VOD content and protected videos are not supported
"""

import json
import re
import subprocess
import traceback
from urllib.parse import urlparse, urlunparse, parse_qs
from functools import lru_cache

from streamlink.logger import getLogger
from streamlink.plugin import Plugin, PluginError, pluginmatcher
from streamlink.plugin.api import useragents, validate
from streamlink.stream.ffmpegmux import MuxedStream
from streamlink.stream.hls import HLSStream
from streamlink.stream.http import HTTPStream
from streamlink.utils.data import search_dict
from streamlink.utils.parse import parse_json


log = getLogger(__name__)


@pluginmatcher(
    name="default",
    pattern=re.compile(
        r"https?://(?:\w+\.)?youtube\.com/(?:v/|live/|watch\?(?:.*&)?v=)(?P<video_id>[\w-]{11})",
    ),
)
@pluginmatcher(
    name="channel",
    pattern=re.compile(
        r"https?://(?:\w+\.)?youtube\.com/(?:@|c(?:hannel)?/|user/)?(?P<channel>[^/?]+)(?P<live>/live)?/?$",
    ),
)
@pluginmatcher(
    name="channel_id",
    pattern=re.compile(
        r"https?://(?:\w+\.)?youtube\.com/channel/(?P<channel_id>[^/?]+)(?P<live>/live)?/?$",
    ),
)
@pluginmatcher(
    name="embed",
    pattern=re.compile(
        r"https?://(?:\w+\.)?youtube\.com/embed/(?:live_stream\?channel=(?P<live_channel>[^/?&]+)|(?P<video_id>[\w-]{11}))",
    ),
)
@pluginmatcher(
    name="shorthand",
    pattern=re.compile(
        r"https?://youtu\.be/(?P<video_id>[\w-]{11})",
    ),
)
@pluginmatcher(
    name="playlist",
    pattern=re.compile(
        r"https?://(?:\w+\.)?youtube\.com/playlist\?list=(?P<playlist_id>[\w-]+)",
    ),
)
@pluginmatcher(
    name="profile_playlists",
    pattern=re.compile(
        r"https?://(?:\w+\.)?youtube\.com/(?:@|c(?:hannel)?/|user/)?(?P<username>[^/]+)/playlists",
    ),
)
@pluginmatcher(
    name="profile_shorts",
    pattern=re.compile(
        r"https?://(?:\w+\.)?youtube\.com/(?:@|c(?:hannel)?/|user/)?(?P<username>[^/]+)/shorts",
    ),
)
@pluginmatcher(
    name="shorts",
    pattern=re.compile(
        r"https?://(?:\w+\.)?youtube\.com/shorts/(?P<video_id>[\w-]{11})",
    ),
)
class YouTube(Plugin):
    _re_ytInitialData = re.compile(r"var\s+ytInitialData\s*=\s*({.*?})\s*;</script>", re.DOTALL)
    _re_ytInitialPlayerResponse = re.compile(r"var\s+ytInitialPlayerResponse\s*=\s*({.*?});\s*var\s+\w+\s*=", re.DOTALL)

    _url_canonical = "https://www.youtube.com/watch?v={video_id}"
    _url_channelid_live = "https://www.youtube.com/channel/{channel_id}/live"
    
    # Cache for API responses
    _cache = {}
    _cache_timeout = 300  # 5 minutes

    # Pre-compiled regex patterns for performance
    _re_video_id = re.compile(r'"videoId"\s*:\s*"([\w-]{11})"')
    _re_watch_link = re.compile(r'/watch\?v=([\w-]{11})')
    _re_api_key = re.compile(r"""(?P<q1>["'])INNERTUBE_API_KEY(?P=q1)\s*:\s*(?P<q2>["'])(?P<data>.+?)(?P=q2)""")
    
    # Optimized yt-dlp command template
    YTDLP_CMD_TEMPLATE = [
        "python3", "-m", "yt_dlp",
        "--no-playlist",
        "--skip-download",
        "--dump-single-json",
        "--format", "best[ext=m3u8]/best",
        "--no-warnings",
        "--quiet",
        "--extractor-args", "youtube:skip=webpage;player_client=android",
    ]

    adp_video = {
        137: "1080p", 299: "1080p60", 264: "1440p", 308: "1440p60",
        266: "2160p", 315: "2160p60", 138: "2160p", 302: "720p60",
        135: "480p", 133: "240p", 160: "144p",
    }
    adp_audio = {
        140: 128, 141: 256, 171: 128, 249: 48,
        250: 64, 251: 160, 256: 256, 258: 258,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._normalize_url()
        self.session.http.headers.update({"User-Agent": useragents.CHROME})
        self.session.http.timeout = 15  # Set global timeout

    def _normalize_url(self):
        """Normalize YouTube URLs to a standard format"""
        parsed = urlparse(self.url)

        # Handle different YouTube domains
        if parsed.netloc in ("gaming.youtube.com", "m.youtube.com"):
            self.url = urlunparse(parsed._replace(scheme="https", netloc="www.youtube.com"))
        elif self.matches["shorthand"]:
            self.url = self._url_canonical.format(video_id=self.match["video_id"])
        elif self.matches["embed"] and self.match["video_id"]:
            self.url = self._url_canonical.format(video_id=self.match["video_id"])
        elif self.matches["embed"] and self.match["live_channel"]:
            self.url = self._url_channelid_live.format(channel_id=self.match["live_channel"])
        elif self.matches["shorts"]:
            self.url = self._url_canonical.format(video_id=self.match["video_id"])
        elif parsed.scheme != "https":
            self.url = urlunparse(parsed._replace(scheme="https"))

    @classmethod
    def stream_weight(cls, stream: str) -> tuple[float, str]:
        match_hfr = re.match(r"(\d+p)(\d+)", stream)
        if match_hfr:
            weight, group = Plugin.stream_weight(match_hfr.group(1))
            return weight + 1, "high_frame_rate"
        return Plugin.stream_weight(stream)

    @staticmethod
    def _schema_consent(data):
        schema_consent = validate.Schema(
            validate.parse_html(),
            validate.any(
                validate.xml_find(".//form[@action='https://consent.youtube.com/s']"),
                validate.all(
                    validate.xml_xpath(".//form[@action='https://consent.youtube.com/save']"),
                    validate.filter(lambda elem: elem.xpath(".//input[@type='hidden'][@name='set_ytc'][@value='true']")),
                    validate.get(0),
                ),
            ),
            validate.union((
                validate.get("action"),
                validate.xml_xpath(".//input[@type='hidden']"),
            )),
        )
        return schema_consent.validate(data)

    def _schema_canonical(self, data):
        schema_canonical = validate.Schema(
            validate.parse_html(),
            validate.xml_xpath_string(".//link[@rel='canonical'][1]/@href"),
            validate.regex(self.matchers["default"].pattern),
            validate.get("video_id"),
        )
        return schema_canonical.validate(data)

    @classmethod
    def _schema_playabilitystatus(cls, data):
        schema = validate.Schema(
            {
                "playabilityStatus": {
                    "status": str,
                    validate.optional("reason"): validate.any(str, None),
                },
            },
            validate.get("playabilityStatus"),
            validate.union_get("status", "reason"),
        )
        return schema.validate(data)

    @classmethod
    def _schema_videodetails(cls, data):
        schema = validate.Schema(
            {
                "videoDetails": {
                    "videoId": str,
                    "author": str,
                    "title": str,
                    validate.optional("isLive"): validate.transform(bool),
                    validate.optional("isLiveContent"): validate.transform(bool),
                    validate.optional("isLiveDvrEnabled"): validate.transform(bool),
                    validate.optional("isLowLatencyLiveStream"): validate.transform(bool),
                    validate.optional("isPrivate"): validate.transform(bool),
                },
                "microformat": validate.all(
                    validate.any(
                        validate.all(
                            {"playerMicroformatRenderer": dict},
                            validate.get("playerMicroformatRenderer"),
                        ),
                        validate.all(
                            {"microformatDataRenderer": dict},
                            validate.get("microformatDataRenderer"),
                        ),
                    ),
                    {validate.optional("category"): str},
                ),
            },
            validate.union_get(
                ("videoDetails", "videoId"),
                ("videoDetails", "author"),
                ("microformat", "category"),
                ("videoDetails", "title"),
                ("videoDetails", "isLive"),
            ),
        )
        videoDetails = schema.validate(data)
        log.trace(f"videoDetails = {videoDetails!r}")
        return videoDetails

    @classmethod
    def _schema_streamingdata(cls, data):
        schema = validate.Schema(
            {
                "streamingData": {
                    validate.optional("hlsManifestUrl"): str,
                    validate.optional("formats"): [
                        validate.all(
                            {
                                "itag": int,
                                "qualityLabel": str,
                                validate.optional("url"): validate.url(scheme="http"),
                            },
                            validate.union_get("url", "qualityLabel"),
                        ),
                    ],
                    validate.optional("adaptiveFormats"): [
                        validate.all(
                            {
                                "itag": int,
                                "mimeType": validate.all(
                                    str,
                                    validate.regex(
                                        re.compile(r"""^(?P<type>\w+)/(?P<container>\w+); codecs="(?P<codecs>.+)"$"""),
                                    ),
                                    validate.union_get("type", "codecs"),
                                ),
                                validate.optional("url"): validate.url(scheme="http"),
                                validate.optional("qualityLabel"): str,
                            },
                            validate.union_get("url", "qualityLabel", "itag", "mimeType"),
                        ),
                    ],
                },
            },
            validate.get("streamingData"),
            validate.union_get("hlsManifestUrl", "formats", "adaptiveFormats"),
        )
        hls_manifest, formats, adaptive_formats = schema.validate(data)
        return hls_manifest, formats or [], adaptive_formats or []

    def _create_adaptive_streams(self, adaptive_formats):
        """Create adaptive streams with optimized audio selection"""
        streams = {}
        adaptive_streams = {}
        audio_streams = {}
        best_audio_itag = None

        # Extract audio streams from the adaptive format list
        for url, _label, itag, mimeType in adaptive_formats:
            if url is None:
                continue

            adaptive_streams[itag] = url
            stream_type, stream_codec = mimeType
            stream_codec = re.sub(r"^(\w+).*$", r"\1", stream_codec)

            if stream_type == "audio" and itag in self.adp_audio:
                audio_bitrate = self.adp_audio[itag]
                if stream_codec not in audio_streams or audio_bitrate > self.adp_audio[audio_streams[stream_codec]]:
                    audio_streams[stream_codec] = itag

                if best_audio_itag is None or audio_bitrate > self.adp_audio[best_audio_itag]:
                    best_audio_itag = itag

        if not best_audio_itag:
            return {}

        # Check if best audio stream is accessible
        if self.session.http.head(adaptive_streams[best_audio_itag], raise_for_status=False).status_code >= 400:
            return {}

        streams.update({
            f"audio_{stream_codec}": HTTPStream(self.session, adaptive_streams[itag])
            for stream_codec, itag in audio_streams.items()
        })

        if best_audio_itag and adaptive_streams and MuxedStream.is_usable(self.session):
            aurl = adaptive_streams[best_audio_itag]
            for itag, name in self.adp_video.items():
                if itag not in adaptive_streams:
                    continue
                vurl = adaptive_streams[itag]
                log.debug(f"MuxedStream: v {itag} a {best_audio_itag} = {name}")
                streams[name] = MuxedStream(
                    self.session,
                    HTTPStream(self.session, vurl),
                    HTTPStream(self.session, aurl),
                )

        return streams

    def _get_res(self, url, cache_key=None):
        """Fetch URL with consent handling and optional caching"""
        if cache_key and cache_key in self._cache:
            return self._cache[cache_key]

        res = self.session.http.get(url)
        
        if urlparse(res.url).netloc == "consent.youtube.com":
            target, elems = self._schema_consent(res.text)
            c_data = {elem.attrib.get("name"): elem.attrib.get("value") for elem in elems}
            log.debug(f"Consent required - target: {target}")
            res = self.session.http.post(target, data=c_data)

        if cache_key:
            self._cache[cache_key] = res

        return res

    def _get_data_from_regex(self, res, regex, descr):
        """Extract data using regex with error handling"""
        match = re.search(regex, res.text)
        if not match:
            log.debug(f"Missing {descr}")
            return None
        return parse_json(match.group(1))

    def _get_data_from_api(self, video_id):
        """Fetch data from YouTube API with caching"""
        cache_key = f"api_{video_id}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            # Try to get API key from page first
            res = self._get_res(self._url_canonical.format(video_id=video_id), f"page_{video_id}")
            
            api_key = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"  # Default key
            if m := self._re_api_key.search(res.text):
                api_key = m.group("data")

            data = self.session.http.post(
                "https://www.youtube.com/youtubei/v1/player",
                headers={"Content-Type": "application/json"},
                params={"key": api_key},
                json={
                    "videoId": video_id,
                    "contentCheckOk": True,
                    "racyCheckOk": True,
                    "context": {
                        "client": {
                            "clientName": "ANDROID",
                            "clientVersion": "21.08.266",
                            "platform": "DESKTOP",
                            "clientScreen": "EMBED",
                        },
                        "user": {"lockedSafetyMode": "false"},
                        "request": {"useSsl": "true"},
                    },
                },
                schema=validate.Schema(validate.parse_json()),
            )
            
            self._cache[cache_key] = data
            return data
            
        except Exception as e:
            log.debug(f"API request failed: {e}")
            return None

    @staticmethod
    def _data_video_id(data):
        """Extract video ID from data structure"""
        if not data:
            return None
        for key in ("videoRenderer", "gridVideoRenderer", "playlistVideoRenderer"):
            for videoRenderer in search_dict(data, key):
                videoId = videoRenderer.get("videoId")
                if videoId:
                    return videoId
        return None

    def _get_channel_live_video_id(self, channel_id):
        """Extract live video ID from channel page (optimized)"""
        channel_url = self._url_channelid_live.format(channel_id=channel_id)
        log.debug(f"Resolving channel live page: {channel_url}")

        try:
            res = self._get_res(channel_url, f"channel_{channel_id}")
            content = res.text

            # Optimized single regex pattern for live video
            patterns = [
                r'"videoId":"([\w-]{11})".*?"isLive":\s*true',
                r'"videoId":"([\w-]{11})"',
            ]

            for pattern in patterns:
                if match := re.search(pattern, content):
                    video_id = match.group(1)
                    log.debug(f"Found video ID: {video_id}")
                    return video_id

            log.debug("No video ID found in channel page")
            return None

        except Exception as e:
            log.error(f"Error fetching channel page: {e}")
            return None

    def _get_streams_ytdlp(self, url, is_live=False):
        """Optimized yt-dlp stream extraction"""
        cache_key = f"ytdlp_{url}_{is_live}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            log.info(f"Processing with yt-dlp: {url}")

            # Use optimized command for faster extraction
            cmd = self.YTDLP_CMD_TEMPLATE.copy()
            if is_live:
                cmd[cmd.index("--format") + 1] = "best[ext=m3u8]/best"

            result = subprocess.run(
                cmd + [url],
                capture_output=True,
                text=True,
                timeout=20,  # Reduced timeout
            )

            if result.returncode != 0:
                if "This video is unavailable" in result.stderr:
                    log.error("Video unavailable")
                else:
                    log.error(f"yt-dlp error: {result.stderr[:200]}")
                return None

            info = json.loads(result.stdout)

            # Set metadata
            self.id = info.get("id")
            self.author = info.get("uploader", info.get("channel", "Unknown"))
            self.title = info.get("title", "YouTube Video")
            self.category = info.get("categories", ["Unknown"])[0] if info.get("categories") else "Unknown"

            is_live = info.get("is_live", False)
            log.info(f"{'Live Stream' if is_live else 'Video'}: {self.title}")

            # Extract streams
            streams = {}
            headers = {
                "User-Agent": useragents.CHROME,
                "Referer": "https://www.youtube.com/",
                "Origin": "https://www.youtube.com",
            }

            # Check for direct URL first
            if url := info.get("url"):
                if "m3u8" in url:
                    if is_live:
                        streams["live"] = HLSStream(
                            self.session, url, headers=headers,
                            live_edge=2, segment_threads=2,
                            force_restart=True, timeout=15
                        )
                    else:
                        streams.update(HLSStream.parse_variant_playlist(
                            self.session, url, headers=headers, name_key="pixels"
                        ))
                else:
                    streams["best"] = HTTPStream(self.session, url, headers=headers)

            # Check formats if no direct URL
            if not streams and (formats := info.get("formats", [])):
                for fmt in formats:
                    if fmt_url := fmt.get("url"):
                        if "m3u8" in fmt_url:
                            if is_live:
                                streams["live"] = HLSStream(
                                    self.session, fmt_url, headers=headers,
                                    live_edge=2, segment_threads=2,
                                    force_restart=True, timeout=15
                                )
                                break
                            else:
                                streams.update(HLSStream.parse_variant_playlist(
                                    self.session, fmt_url, headers=headers, name_key="pixels"
                                ))
                                break
                        else:
                            quality = f"{fmt.get('height', 0)}p" if fmt.get('height', 0) > 0 else fmt.get("format_note", "unknown")
                            streams[quality] = HTTPStream(self.session, fmt_url, headers=headers)

            self._cache[cache_key] = streams
            log.debug(f"Found {len(streams)} streams via yt-dlp")
            return streams

        except subprocess.TimeoutExpired:
            log.error("yt-dlp timeout")
            return None
        except Exception as e:
            log.error(f"yt-dlp error: {e}")
            return None

    def _extract_video_from_playlist(self):
        """Optimized playlist extraction"""
        try:
            log.info(f"Extracting video from playlist: {self.url}")

            # Try to get from cache first
            cache_key = f"playlist_{self.url}"
            if cache_key in self._cache:
                return self._cache[cache_key]

            res = self._get_res(self.url)
            content = res.text

            # Try multiple methods in order of speed
            video_id = None

            # Method 1: ytInitialData (fastest)
            if match := re.search(self._re_ytInitialData, content):
                try:
                    initial_data = parse_json(match.group(1))
                    video_id = self._data_video_id(initial_data)
                except Exception:
                    pass

            # Method 2: Direct videoId pattern
            if not video_id:
                if match := self._re_video_id.search(content):
                    video_id = match.group(1)

            # Method 3: Watch links
            if not video_id:
                if match := self._re_watch_link.search(content):
                    video_id = match.group(1)

            # Method 4: yt-dlp fallback
            if not video_id:
                log.debug("Using yt-dlp for playlist")
                cmd = [
                    "python3", "-m", "yt_dlp",
                    "--flat-playlist",
                    "--playlist-end", "1",
                    "--get-id",
                    "--no-warnings",
                    "--quiet",
                    self.url
                ]
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                    if result.returncode == 0 and (output := result.stdout.strip()):
                        video_id = output
                except Exception:
                    pass

            if not video_id:
                log.error("No videos found in playlist")
                return None

            log.info(f"Using first video from playlist: {video_id}")
            self.url = self._url_canonical.format(video_id=video_id)
            self._cache[cache_key] = True
            return True

        except Exception as e:
            log.error(f"Error extracting playlist: {e}")
            return None

    def _handle_profile_url(self, url_type):
        """Handle profile URLs (shorts/playlists) - unified method"""
        try:
            log.info(f"Handling profile {url_type} URL: {self.url}")

            res = self._get_res(self.url)
            content = res.text

            pattern = r'/(?:shorts|watch)\?v=([\w-]{11})' if url_type == "shorts" else r'/playlist\?list=([\w-]+)'
            
            if matches := re.findall(pattern, content):
                if url_type == "shorts":
                    self.url = self._url_canonical.format(video_id=matches[0])
                else:
                    self.url = f"https://www.youtube.com/playlist?list={matches[0]}"
                    return self._extract_video_from_playlist()
                return True

            log.error(f"No {url_type} found on profile page")
            return None

        except Exception as e:
            log.error(f"Error handling profile {url_type}: {e}")
            return None

    def _get_streams(self):
        """Main method - optimized hybrid approach"""
        
        # Get match groups safely
        match_dict = self.match.groupdict() if self.match else {}
        
        if self.matches:
            log.debug(f"Matchers: {[name for name in self.matches]}")

        # Handle different URL types
        channel_id = match_dict.get("channel_id") or match_dict.get("channel")
        live_suffix = match_dict.get("live")

        # Channel live URLs
        if channel_id and live_suffix:
            log.info(f"Channel live URL detected: {channel_id}")
            if resolved_id := self._get_channel_live_video_id(channel_id):
                self.url = self._url_canonical.format(video_id=resolved_id)
            else:
                return self._get_streams_ytdlp(self.url, is_live=True)

        # Embed live URLs
        if self.matches["embed"] and match_dict.get("live_channel"):
            channel_id = match_dict["live_channel"]
            log.info(f"Embed live URL detected: {channel_id}")
            if resolved_id := self._get_channel_live_video_id(channel_id):
                self.url = self._url_canonical.format(video_id=resolved_id)
            else:
                return self._get_streams_ytdlp(self.url, is_live=True)

        # Profile URLs
        if self.matches["profile_shorts"]:
            if not self._handle_profile_url("shorts"):
                raise PluginError("Could not extract short video from profile")

        if self.matches["profile_playlists"]:
            if not self._handle_profile_url("playlists"):
                raise PluginError("Could not extract playlist from profile")

        # Playlist URLs
        if self.matches["playlist"]:
            if not self._extract_video_from_playlist():
                return self._get_streams_ytdlp(self.url)
            
        # Strip playlist parameter from watch URLs
        parsed = urlparse(self.url)
        if parsed.path.startswith('/watch'):
            query = parse_qs(parsed.query)
            if 'list' in query and 'v' in query:
                self.url = self._url_canonical.format(video_id=query['v'][0])

        # Main extraction logic
        try:
            video_id = None
            
            # Extract video ID from URL
            if "video_id" in match_dict:
                video_id = match_dict["video_id"]
            elif self.matches["channel"]:
                res = self._get_res(self.url)
                if initial := self._get_data_from_regex(res, self._re_ytInitialData, "initial data"):
                    video_id = self._data_video_id(initial)

            if not video_id:
                log.error("Could not extract video ID")
                return self._get_streams_ytdlp(self.url)

            # Try API first (fastest for VOD)
            if data := self._get_data_from_api(video_id):
                status, reason = self._schema_playabilitystatus(data)
                
                if status == "OK" and not reason:
                    # Get metadata from initial player response
                    res = self._get_res(self._url_canonical.format(video_id=video_id))
                    if init_data := self._get_data_from_regex(res, self._re_ytInitialPlayerResponse, "player response"):
                        try:
                            self.id, self.author, self.category, self.title, is_live = self._schema_videodetails(init_data)
                            log.debug(f"Video is live: {is_live}")
                        except Exception as e:
                            log.debug(f"Could not get video details: {e}")
                            is_live = False

                    # Parse streaming data
                    hls_manifest, formats, adaptive_formats = self._schema_streamingdata(data)

                    streams = {}
                    
                    # Add progressive formats
                    for url, label in formats:
                        if url and self.session.http.head(url, raise_for_status=False).status_code < 400:
                            streams[label] = HTTPStream(self.session, url)

                    # Add adaptive streams for VOD
                    if not is_live:
                        streams.update(self._create_adaptive_streams(adaptive_formats))

                    # Add HLS manifest
                    if hls_manifest:
                        try:
                            streams.update(HLSStream.parse_variant_playlist(
                                self.session, hls_manifest, name_key="pixels"
                            ))
                        except Exception as e:
                            log.debug(f"Failed to parse HLS manifest: {e}")

                    if streams:
                        return streams

            # Fallback to yt-dlp
            log.info("Native method failed, trying yt-dlp")
            return self._get_streams_ytdlp(self.url)

        except Exception as e:
            log.error(f"Native method error: {e}")
            log.debug(traceback.format_exc())
            return self._get_streams_ytdlp(self.url)


__plugin__ = YouTube