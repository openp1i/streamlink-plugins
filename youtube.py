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
    name="embed",
    pattern=re.compile(
        r"https?://(?:\w+\.)?youtube\.com/embed/(?:live_stream\?channel=(?P<live>[^/?&]+)|(?P<video_id>[\w-]{11}))",
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
@pluginmatcher(
    name="shorts_playlist",
    pattern=re.compile(
        r"https?://(?:\w+\.)?youtube\.com/shorts/(?P<video_id>[\w-]{11})\?.*",
    ),
)
class YouTube(Plugin):
    _re_ytInitialData = re.compile(r"""var\s+ytInitialData\s*=\s*({.*?})\s*;\s*</script>""", re.DOTALL)
    _re_ytInitialPlayerResponse = re.compile(r"""var\s+ytInitialPlayerResponse\s*=\s*({.*?});\s*var\s+\w+\s*=""", re.DOTALL)

    _url_canonical = "https://www.youtube.com/watch?v={video_id}"
    _url_channelid_live = "https://www.youtube.com/channel/{channel_id}/live"

    # There are missing itags
    adp_video = {
        137: "1080p",
        299: "1080p60",  # HFR
        264: "1440p",
        308: "1440p60",  # HFR
        266: "2160p",
        315: "2160p60",  # HFR
        138: "2160p",
        302: "720p60",  # HFR
        135: "480p",
        133: "240p",
        160: "144p",
    }
    adp_audio = {
        140: 128,
        141: 256,
        171: 128,
        249: 48,
        250: 64,
        251: 160,
        256: 256,
        258: 258,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        parsed = urlparse(self.url)

        # translate input URLs to be able to find embedded data and to avoid unnecessary HTTP redirects
        if parsed.netloc == "gaming.youtube.com":
            self.url = urlunparse(parsed._replace(scheme="https", netloc="www.youtube.com"))
        elif self.matches["shorthand"]:
            self.url = self._url_canonical.format(video_id=self.match["video_id"])
        elif self.matches["embed"] and self.match["video_id"]:
            self.url = self._url_canonical.format(video_id=self.match["video_id"])
        elif self.matches["embed"] and self.match["live"]:
            self.url = self._url_channelid_live.format(channel_id=self.match["live"])
        elif self.matches["shorts"] or self.matches["shorts_playlist"]:
            # Convert shorts URL to regular watch URL
            self.url = self._url_canonical.format(video_id=self.match["video_id"])
        elif parsed.scheme != "https":
            self.url = urlunparse(parsed._replace(scheme="https"))

        self.session.http.headers.update({"User-Agent": useragents.CHROME})

    @classmethod
    def stream_weight(cls, stream: str) -> tuple[float, str]:
        match_3d = re.match(r"(\w+)_3d", stream)
        match_hfr = re.match(r"(\d+p)(\d+)", stream)
        if match_hfr:
            weight, group = Plugin.stream_weight(match_hfr.group(1))
            weight += 1
            group = "high_frame_rate"
        else:
            weight, group = Plugin.stream_weight(stream)

        return weight, group

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
                    {
                        validate.optional("category"): str,
                    },
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
        streams = {}
        adaptive_streams = {}
        audio_streams = {}
        best_audio_itag = None

        # Extract audio streams from the adaptive format list
        for url, _label, itag, mimeType in adaptive_formats:
            if url is None:
                continue

            # extract any high quality streams only available in adaptive formats
            adaptive_streams[itag] = url
            stream_type, stream_codec = mimeType
            stream_codec = re.sub(r"^(\w+).*$", r"\1", stream_codec)

            if stream_type == "audio" and itag in self.adp_audio:
                audio_bitrate = self.adp_audio[itag]
                if stream_codec not in audio_streams or audio_bitrate > self.adp_audio[audio_streams[stream_codec]]:
                    audio_streams[stream_codec] = itag

                # find the best quality audio stream m4a, opus or vorbis
                if best_audio_itag is None or audio_bitrate > self.adp_audio[best_audio_itag]:
                    best_audio_itag = itag

        if (
            not best_audio_itag
            or self.session.http.head(adaptive_streams[best_audio_itag], raise_for_status=False).status_code >= 400
        ):
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

    def _get_res(self, url):
        res = self.session.http.get(url)
        if urlparse(res.url).netloc == "consent.youtube.com":
            target, elems = self._schema_consent(res.text)
            c_data = {
                elem.attrib.get("name"): elem.attrib.get("value")
                for elem in elems
            }  # fmt: skip
            log.debug(f"consent target: {target}")
            log.debug(f"consent data: {', '.join(c_data.keys())}")
            res = self.session.http.post(target, data=c_data)
        return res

    @staticmethod
    def _get_data_from_regex(res, regex, descr):
        match = re.search(regex, res.text)
        if not match:
            log.debug(f"Missing {descr}")
            return
        return parse_json(match.group(1))

    def _get_data_from_api(self, res):
        try:
            video_id = self.match["video_id"]
        except (KeyError, TypeError):
            video_id = None

        if video_id is None:
            try:
                video_id = self._schema_canonical(res.text)
            except (PluginError, TypeError):
                return

        if m := re.search(r"""(?P<q1>["'])INNERTUBE_API_KEY(?P=q1)\s*:\s*(?P<q2>["'])(?P<data>.+?)(?P=q2)""", res.text):
            api_key = m.group("data")
        else:
            api_key = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

        return self.session.http.post(
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
                        "clientVersion": "19.45.36",
                        "platform": "DESKTOP",
                        "clientScreen": "EMBED",
                        "clientFormFactor": "UNKNOWN_FORM_FACTOR",
                        "browserName": "Chrome",
                    },
                    "user": {"lockedSafetyMode": "false"},
                    "request": {"useSsl": "true"},
                },
            },
            schema=validate.Schema(
                validate.parse_json(),
            ),
        )

    @staticmethod
    def _data_video_id(data):
        if not data:
            return None
        for key in ("videoRenderer", "gridVideoRenderer"):
            for videoRenderer in search_dict(data, key):
                videoId = videoRenderer.get("videoId")
                if videoId is not None:
                    return videoId

    def _extract_shorts_playlist(self, current_video_id):
        """Extract next video from shorts playlist"""
        try:
            log.info(f"Extracting shorts playlist for video: {current_video_id}")

            # Fetch the shorts page
            url = f"https://www.youtube.com/shorts/{current_video_id}"
            res = self._get_res(url)
            content = res.text

            # Try to find initial data
            match = re.search(self._re_ytInitialData, content)
            if match:
                try:
                    initial_data = parse_json(match.group(1))
                    log.debug("Found initial data, searching for shorts playlist...")

                    # Look for related videos or playlist data
                    # Try different search patterns for YouTube shorts playlist

                    # Method 1: Look for watchNextEndpoint
                    watch_next_videos = search_dict(initial_data, "watchNextEndpoint")
                    if watch_next_videos:
                        for video in watch_next_videos:
                            if isinstance(video, dict) and "videoId" in video:
                                next_video_id = video["videoId"]
                                log.debug(f"Found next video via watchNextEndpoint: {next_video_id}")
                                return next_video_id

                    # Method 2: Look for related videos
                    related_videos = search_dict(initial_data, "richItemRenderer")
                    if related_videos:
                        for video in related_videos:
                            if isinstance(video, dict):
                                # Try to find videoId in various nested structures
                                video_id = self._data_video_id([video])
                                if video_id and video_id != current_video_id:
                                    log.debug(f"Found related video: {video_id}")
                                    return video_id

                    # Method 3: Look for shorts shelf
                    shorts_shelves = search_dict(initial_data, "shortsShelfRenderer")
                    if shorts_shelves:
                        for shelf in shorts_shelves:
                            if isinstance(shelf, dict) and "items" in shelf:
                                items = shelf["items"]
                                if items and isinstance(items, list):
                                    # Find current video index and get next one
                                    for i, item in enumerate(items):
                                        item_video_id = self._data_video_id([item])
                                        if item_video_id == current_video_id and i < len(items) - 1:
                                            next_item = items[i + 1]
                                            next_video_id = self._data_video_id([next_item])
                                            if next_video_id:
                                                log.debug(f"Found next video in shorts shelf: {next_video_id}")
                                                return next_video_id

                except Exception as e:
                    log.debug(f"Failed to parse shorts playlist data: {e}")

            # Method 4: Fallback to yt-dlp
            log.debug("Trying yt-dlp for shorts playlist...")
            try:
                cmd = [
                    "python3", "-m", "yt_dlp",
                    "--no-playlist",
                    "--skip-download",
                    "--dump-json",
                    "--no-warnings",
                    "--quiet",
                    f"https://www.youtube.com/shorts/{current_video_id}"
                ]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=15
                )

                if result.returncode == 0:
                    info = json.loads(result.stdout)
                    # Get related videos
                    related_videos = info.get("related_videos", [])
                    if related_videos:
                        for video in related_videos:
                            if isinstance(video, dict) and "id" in video:
                                next_video_id = video["id"]
                                if next_video_id != current_video_id:
                                    log.debug(f"Found next video via yt-dlp: {next_video_id}")
                                    return next_video_id
            except Exception as e:
                log.debug(f"yt-dlp fallback failed: {e}")

            log.debug("No next video found in shorts playlist")
            return None

        except Exception as e:
            log.error(f"Error extracting shorts playlist: {e}")
            log.debug(f"Traceback: {traceback.format_exc()}")
            return None

    def _extract_video_from_playlist(self):
        """Extract first video from playlist by fetching the page directly"""
        try:
            log.info(f"Extracting video from playlist: {self.url}")

            # Use the existing _get_res method which already handles consent
            res = self._get_res(self.url)

            log.debug(f"Playlist page status: {res.status_code}, URL: {res.url}")

            # Try to decode the content if it's compressed
            content = res.text

            # If content is very short or seems binary, try a different approach
            if len(content) < 100 or any(ord(c) < 32 and c not in '\n\r\t' for c in content[:100]):
                log.debug("Content appears to be binary/compressed, trying raw response...")
                # Try to get the raw bytes and decode as utf-8
                try:
                    content = res.content.decode('utf-8', errors='ignore')
                except:
                    # If that fails, try latin-1
                    content = res.content.decode('latin-1', errors='ignore')

            # Try multiple methods to extract video data
            video_id = None

            # Method 1: Try ytInitialData regex (the main method)
            match = re.search(self._re_ytInitialData, content)
            if match:
                try:
                    initial_data = parse_json(match.group(1))
                    log.debug("Found initial data via regex, searching for videos...")

                    # Search for video IDs in the playlist data
                    video_id = self._data_video_id(initial_data)

                    if not video_id:
                        # Try an alternative search pattern for playlists
                        log.debug("Trying alternative search for playlist videos...")

                        # Look for playlistVideoRenderer
                        playlist_items = search_dict(initial_data, "playlistVideoRenderer")
                        log.debug(f"Found {len(playlist_items)} playlistVideoRenderer items")

                        if playlist_items:
                            for item in playlist_items:
                                if "videoId" in item:
                                    video_id = item["videoId"]
                                    log.debug(f"Found videoId from playlistVideoRenderer: {video_id}")
                                    break
                except Exception as e:
                    log.debug(f"Failed to parse initial data: {e}")

            # Method 2: Look for the first video ID pattern in the content
            if not video_id:
                log.debug("Trying direct search for videoId patterns...")
                # Look for patterns like: "videoId":"XXXXXXXXXXX"
                video_id_match = re.search(r'"videoId"\s*:\s*"([\w-]{11})"', content)
                if video_id_match:
                    video_id = video_id_match.group(1)
                    log.debug(f"Found videoId via pattern: {video_id}")

            # Method 3: Look for watch links
            if not video_id:
                log.debug("Looking for /watch links...")
                # Find all watch links and take the first one
                watch_matches = re.findall(r'/watch\?v=([\w-]{11})', content)
                if watch_matches:
                    video_id = watch_matches[0]
                    log.debug(f"Found videoId via /watch link: {video_id}")

            # Method 4: Try to find any video metadata
            if not video_id:
                log.debug("Looking for video metadata...")
                # Try to find videoRenderer objects
                video_renderer_match = re.search(r'"videoRenderer"\s*:\s*\{[^}]*"videoId"\s*:\s*"([\w-]{11})"', content)
                if video_renderer_match:
                    video_id = video_renderer_match.group(1)
                    log.debug(f"Found videoId in videoRenderer: {video_id}")

            # Method 5: Last resort - use yt-dlp with increased timeout
            if not video_id:
                log.debug("Trying yt-dlp as fallback...")
                try:
                    cmd = [
                        "python3", "-m", "yt_dlp",
                        "--flat-playlist",
                        "--playlist-start", "1",
                        "--playlist-end", "1",
                        "--get-id",
                        "--no-warnings",
                        "--quiet",
                        self.url
                    ]

                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=45  # Increased timeout
                    )

                    if result.returncode == 0:
                        video_ids = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
                        if video_ids:
                            video_id = video_ids[0]
                            log.debug(f"Found videoId via yt-dlp fallback: {video_id}")
                except Exception as e:
                    log.debug(f"yt-dlp fallback failed: {e}")

            if not video_id:
                log.error("No videos found in playlist data using any method")
                return None

            log.info(f"Using first video from playlist: {video_id}")

            # Update URL to the first video
            self.url = self._url_canonical.format(video_id=video_id)
            return True

        except Exception as e:
            log.error(f"Error extracting playlist: {e}")
            log.debug(f"Traceback: {traceback.format_exc()}")

        return None

    def _handle_profile_playlists(self):
        """Handle @username/playlists URLs by extracting first playlist"""
        try:
            log.info(f"Handling profile playlists URL: {self.url}")

            # Fetch the profile playlists page
            res = self._get_res(self.url)
            content = res.text

            # Try to find playlist links
            playlist_matches = re.findall(r'/playlist\?list=([\w-]+)', content)
            if playlist_matches:
                first_playlist_id = playlist_matches[0]
                log.info(f"Found playlist: {first_playlist_id}")

                # Convert to playlist URL
                self.url = f"https://www.youtube.com/playlist?list={first_playlist_id}"

                # Now extract first video from this playlist
                return self._extract_video_from_playlist()
            else:
                log.error("No playlists found on profile page")
                return None

        except Exception as e:
            log.error(f"Error handling profile playlists: {e}")
            return None

    def _handle_profile_shorts(self):
        """Handle @username/shorts URLs by extracting first short video"""
        try:
            log.info(f"Handling profile shorts URL: {self.url}")

            # Fetch the profile shorts page
            res = self._get_res(self.url)
            content = res.text

            # Try to find shorts video links
            shorts_matches = re.findall(r'/shorts/([\w-]{11})', content)
            if shorts_matches:
                first_short_id = shorts_matches[0]
                log.info(f"Found short video: {first_short_id}")

                # Convert to regular watch URL
                self.url = self._url_canonical.format(video_id=first_short_id)
                return True
            else:
                # Try alternative search for video IDs
                video_id_match = re.search(r'"videoId":"([\w-]{11})"', content)
                if video_id_match:
                    first_short_id = video_id_match.group(1)
                    log.info(f"Found video ID via pattern: {first_short_id}")

                    # Convert to regular watch URL
                    self.url = self._url_canonical.format(video_id=first_short_id)
                    return True

            log.error("No shorts videos found on profile page")
            return None

        except Exception as e:
            log.error(f"Error handling profile shorts: {e}")
            return None

    def _get_streams_ytdlp_live_only(self):
        """Use yt-dlp ONLY for confirmed live streams - simplified version"""
        try:
            # Check if this is a live-specific URL
            is_live_url = (
                self.matches["channel"] and self.match["live"] or
                self.matches["embed"] and self.match["live"] or
                "/live/" in self.url
            )

            if not is_live_url:
                # Not a live-specific URL, don't use yt-dlp
                return None

            # Strip playlist parameter
            url = re.sub(r'(&|\?)list=[^&]+', '', self.url)

            log.info(f"Processing YouTube LIVE URL with yt-dlp: {url}")

            # Use best HLS format for live
            format_str = "best[ext=m3u8]/best"

            cmd = [
                "python3", "-m", "yt_dlp",
                "--no-playlist",
                "--skip-download",
                "--dump-single-json",
                "--format", format_str,
                url
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=25  # Increased timeout
            )

            if result.returncode != 0:
                log.error(f"yt-dlp error for live: {result.stderr[:500]}")
                # Fall back to native method
                return None

            info = json.loads(result.stdout)

            # Double-check it's actually live
            is_live = info.get("is_live", False)
            if not is_live:
                log.info("Not actually live, falling back to native method")
                return None

            # Get HLS URL
            hls_url = info.get("url")

            # If not HLS, check formats
            if not hls_url or "m3u8" not in hls_url:
                formats = info.get("formats", [])
                for fmt in formats:
                    url_val = fmt.get("url")
                    if url_val and "m3u8" in url_val:
                        hls_url = url_val
                        break

            if not hls_url:
                log.error("No HLS stream found for live")
                return None

            # Set metadata
            self.id = info.get("id")
            self.author = info.get("uploader", info.get("channel", "Unknown"))
            self.title = info.get("title", "YouTube Live Stream")
            self.category = info.get("categories", ["Unknown"])[0] if info.get("categories") else "Unknown"

            log.info(f"Live Stream: {self.title} | Author: {self.author}")

            # Create direct HLS stream (no variant parsing)
            headers = {
                "User-Agent": useragents.CHROME,
                "Referer": "https://www.youtube.com/",
                "Origin": "https://www.youtube.com",
            }

            # Always return a single "live" stream
            return {"live": HLSStream(
                self.session,
                hls_url,
                headers=headers,
                live_edge=2,
                segment_threads=2,
                force_restart=True,
                timeout=15
            )}

        except subprocess.TimeoutExpired:
            log.error("yt-dlp timeout for live")
            return None
        except json.JSONDecodeError as e:
            log.error(f"Failed to parse yt-dlp JSON for live: {e}")
            return None
        except Exception as e:
            log.error(f"yt-dlp error for live: {e}")
            return None

    def _get_streams(self):
        """Main method - hybrid approach"""

        # Handle profile shorts URLs first (@username/shorts)
        if self.matches["profile_shorts"]:
            log.info("Profile shorts URL detected, extracting first short video...")
            if not self._handle_profile_shorts():
                raise PluginError("Could not extract short video from profile")
            # After extraction, continue with video processing

        # Handle profile playlists URLs (@username/playlists)
        if self.matches["profile_playlists"]:
            log.info("Profile playlists URL detected, extracting first playlist...")
            if not self._handle_profile_playlists():
                raise PluginError("Could not extract playlist from profile")
            # After extraction, continue with playlist processing

        # Handle playlist URLs
        if self.matches["playlist"]:
            log.info("Playlist URL detected, extracting first video...")
            if not self._extract_video_from_playlist():
                # FALLBACK: If all extraction methods fail, try a simple approach
                log.info("All extraction methods failed, trying simple fallback...")
                raise PluginError(
                    "Could not extract video from playlist. "
                    "Try using a direct video URL instead of playlist URL."
                )
            # After extraction, continue with regular video processing

        # Check if it's a watch URL with playlist parameter
        parsed = urlparse(self.url)
        if parsed.path.startswith('/watch'):
            query = parse_qs(parsed.query)
            if 'list' in query and 'v' in query:
                # This is a video URL with playlist parameter
                # Strip the playlist parameter for cleaner processing
                log.info("Video URL with playlist parameter detected, stripping playlist...")
                # Keep only the video ID parameter
                self.url = self._url_canonical.format(video_id=query['v'][0])

        # FIRST: Try yt-dlp ONLY for confirmed live streams
        # (channel/live, embed/live, or URLs containing /live/)
        if (self.matches["channel"] and self.match["live"]) or \
           (self.matches["embed"] and self.match["live"]) or \
           "/live/" in self.url:
            log.info("Live URL detected, trying yt-dlp first...")
            streams = self._get_streams_ytdlp_live_only()
            if streams:
                log.info(f"Successfully retrieved live streams using yt-dlp")
                return streams
            else:
                log.info("yt-dlp failed for live, falling back to native method")

        # SECOND: For everything else (VOD, playlists, regular videos),
        # use the official plugin's native method
        log.info("Using official plugin method for VOD/regular videos")

        res = self._get_res(self.url)

        if self.matches["channel"] and not self.match["live"]:
            initial = self._get_data_from_regex(res, self._re_ytInitialData, "initial data")
            video_id = self._data_video_id(initial)
            if video_id is None:
                log.error("Could not find videoId on channel page")
                return
            self.url = self._url_canonical.format(video_id=video_id)
            res = self._get_res(self.url)

        if not (data := self._get_data_from_api(res)):
            return
        status, reason = self._schema_playabilitystatus(data)
        # assume that there's an error if reason is set (status will still be "OK" for some reason)
        if status != "OK" or reason:
            log.error(f"Could not get video info - {status}: {reason}")
            return

        # the initial player response contains the category data, which the API response does not
        init_player_response = self._get_data_from_regex(res, self._re_ytInitialPlayerResponse, "initial player response")
        if init_player_response:
            try:
                self.id, self.author, self.category, self.title, is_live = self._schema_videodetails(init_player_response)
                log.debug(f"Video is live: {is_live}")
            except Exception as e:
                log.debug(f"Could not get video details: {e}")
                is_live = False
        else:
            is_live = False

        log.debug(f"Using video ID: {self.id}")

        # TODO: remove parsing of non-HLS stuff, as we don't support this
        streams = {}
        hls_manifest, formats, adaptive_formats = self._schema_streamingdata(data)

        protected = any(url is None for url, *_ in formats + adaptive_formats)
        if protected:
            log.debug("This video may be protected.")

        for url, label in formats:
            if url is None:
                continue
            if self.session.http.head(url, raise_for_status=False).status_code >= 400:
                break
            streams[label] = HTTPStream(self.session, url)

        if not is_live:
            streams.update(self._create_adaptive_streams(adaptive_formats))

        if hls_manifest:
            try:
                hls_streams = HLSStream.parse_variant_playlist(self.session, hls_manifest, name_key="pixels")
                streams.update(hls_streams)
            except Exception as e:
                log.debug(f"Failed to parse HLS manifest: {e}")

        if not streams:
            if protected:
                raise PluginError("This plugin does not support protected videos, try yt-dlp instead")
            if formats or adaptive_formats:
                raise PluginError("This plugin does not support VOD content, try yt-dlp instead")

        return streams


__plugin__ = YouTube
