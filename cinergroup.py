"""
$description Turkish live TV channels from Ciner Group, including Haberturk TV and Show TV.
$url bloomberght.com
$url haberturk.com
$url haberturk.tv
$url showmax.com.tr
$url showturk.com.tr
$url showtv.com.tr
$type live
"""

import re

from streamlink.plugin import Plugin, pluginmatcher
from streamlink.plugin.api import validate
from streamlink.stream.hls import HLSStream


@pluginmatcher(
    name="bloomberght",
    pattern=re.compile(r"https?://(?:www\.)?bloomberght\.com/tv/?"),
)
@pluginmatcher(
    name="haberturk",
    pattern=re.compile(r"https?://(?:www\.)?haberturk\.(?:com|tv)(?:/tv)?/canliyayin/?"),
)
@pluginmatcher(
    name="showmax",
    pattern=re.compile(r"https?://(?:www\.)?showmax\.com\.tr/canli-?yayin/?"),
)
@pluginmatcher(
    name="showturk",
    pattern=re.compile(r"https?://(?:www\.)?showturk\.com\.tr/canli-?yayin(?:/showtv)?/?"),
)
@pluginmatcher(
    name="showtv",
    pattern=re.compile(r"https?://(?:www\.)?showtv\.com\.tr/canli-yayin(?:/showtv)?/?"),
)
class CinerGroup(Plugin):
    @staticmethod
    def _schema_videourl():
        return validate.Schema(
            validate.xml_xpath_string(".//script[contains(text(), 'videoUrl')]/text()"),
            validate.none_or_all(
                re.compile(r"""(?<!//)\s*var\s+videoUrl\s*=\s*(?P<q>['"])(?P<url>.+?)(?P=q)"""),
                validate.none_or_all(
                    validate.get("url"),
                    validate.url(),
                ),
            ),
        )

    @staticmethod
    def _schema_data_ht():
        return validate.Schema(
            validate.xml_xpath_string(".//div[@data-ht][1]/@data-ht"),
            validate.none_or_all(
                validate.parse_json(),
                {
                    "ht_stream_m3u8": validate.url(),
                },
                validate.get("ht_stream_m3u8"),
            ),
        )

    def _get_streams(self):
        # Configure HLS settings for better stability
        self._configure_hls_settings()
        
        root = self.session.http.get(self.url, schema=validate.Schema(validate.parse_html()))
        schema_getters = self._schema_videourl, self._schema_data_ht
        stream_url = next((res for res in (getter().validate(root) for getter in schema_getters) if res), None)

        if stream_url:
            return HLSStream.parse_variant_playlist(self.session, stream_url)
    
    def _configure_hls_settings(self):
        """Configure HLS settings for Turkish streams stability"""
        # Buffer and timeout settings
        self.session.options.set("hls-live-edge", 12)  # Increased buffer size
        self.session.options.set("hls-timeout", 30)  # Longer timeout
        self.session.options.set("hls-segment-timeout", 25)  # Segment timeout
        self.session.options.set("hls-segment-attempts", 6)  # More retries
        self.session.options.set("hls-segment-threads", 4)  # Parallel downloads
        self.session.options.set("hls-playlist-reload-time", "segment")  # Sync with segments
        self.session.options.set("hls-segment-queue-threshold", 5)  # Queue threshold
        
        # Stream settings
        self.session.options.set("stream-timeout", 60)
        self.session.options.set("stream-segment-attempts", 5)
        
        # Buffer settings
        self.session.options.set("ringbuffer-size", 128 * 1024 * 1024)  # 128MB buffer
        self.session.options.set("http-stream-timeout", 30)
        
        # HTTP settings for Turkish CDNs
        self.session.options.set("http-query-param", "cachebust={timestamp}")
        self.session.options.set("http-headers", {
            "Accept": "*/*",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        })


__plugin__ = CinerGroup