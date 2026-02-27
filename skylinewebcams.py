import re
from urllib.parse import urljoin

from streamlink.logger import getLogger
from streamlink.exceptions import PluginError
from streamlink.plugin import Plugin, pluginmatcher
from streamlink.stream.hls import HLSStream

log = getLogger(__name__)


@pluginmatcher(
    re.compile(r"https?://(?:www\.)?skylinewebcams\.com/(?:[\w-]+/){5}.+\.html")
)
class SkylineWebcams(Plugin):
    _source_re = re.compile(r"source:\s*'(.+?)'")
    _HD_AUTH_BASE = "https://hd-auth.skylinewebcams.com/"

    def _get_streams(self):
        res = self.session.http.get(self.url)
        m = self._source_re.search(res.text)
        if m and ".m3u8" in m.group(1):
            url = urljoin(self._HD_AUTH_BASE, m.group(1))
            url = url.replace("livee.", "live.")
            log.debug(url)
            
            # Try to parse as variant playlist first
            try:
                streams = HLSStream.parse_variant_playlist(self.session, url)
                if streams:
                    return streams
            except Exception as e:
                log.debug(f"Failed to parse as variant playlist: {e}")
            
            # Fall back to single stream
            return {"live": HLSStream(self.session, url)}
            
        elif m:
            raise PluginError("Unexpected source format: {0}".format(m.group(1)))
        else:
            raise PluginError("Could not extract source.")


__plugin__ = SkylineWebcams
