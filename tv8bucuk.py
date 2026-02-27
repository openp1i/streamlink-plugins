import re

from streamlink.plugin import Plugin, pluginmatcher
from streamlink.plugin.api import useragents
from streamlink.stream.hls import HLSStream


@pluginmatcher(
    re.compile(r"https?://img\.tv8bucuk\.com/.*tv8-5-canli-yayin.*")
)
class TV8Bucuk(Plugin):

    _HLS_RE = re.compile(
        r'https://tv8\.daioncdn\.net/[^"\']+\.m3u8[^"\']*'
    )

    def _get_streams(self):
        headers = {
            "User-Agent": useragents.CHROME,
            "Referer": "https://img.tv8bucuk.com/",
        }

        # More robust HLS settings
        self.session.options.set("hls-live-edge", 10)  # Increased buffer
        self.session.options.set("hls-timeout", 30)
        self.session.options.set("hls-segment-timeout", 20)
        self.session.options.set("hls-segment-attempts", 5)  # More retries
        self.session.options.set("hls-segment-threads", 3)  # Parallel downloads
        self.session.options.set("hls-playlist-reload-time", "segment")  # Sync with segment duration
        self.session.options.set("stream-segment-attempts", 5)
        self.session.options.set("stream-timeout", 60)
        
        # Disable streamlink's internal buffer which can cause issues
        self.session.options.set("ringbuffer-size", 64 * 1024 * 1024)  # 64MB buffer

        res = self.session.http.get(
            self.url,
            headers=headers,
            acceptable_status=(200,),
        )

        match = self._HLS_RE.search(res.text)
        if not match:
            self.logger.error("TV8.5: HLS URL not found")
            return

        return HLSStream.parse_variant_playlist(
            self.session,
            match.group(0),
            headers=headers,
            namekey="pixels",
        )


__plugin__ = TV8Bucuk