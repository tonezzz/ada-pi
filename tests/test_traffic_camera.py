import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend.traffic_camera import _mjpeg_first_frame


class _DribbleHandler(BaseHTTPRequestHandler):
    """Mimics a dead iTIC cam's mjpeg2.php: trickles ~100B/0.05s with no
    JPEG markers for ~10s, then closes. The old r.read(65536) waited to
    fill the whole buffer, so the socket timeout never fired and a dead
    cam stalled snap() for the server's full stream time (~60s upstream).
    """

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace")
        self.end_headers()
        end = time.time() + 10
        while time.time() < end:
            try:
                self.wfile.write(b"--x\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
            time.sleep(0.05)

    def log_message(self, *a):
        pass


class MjpegDribbleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _DribbleHandler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_dribbling_stream_respects_timeout(self):
        url = f"http://127.0.0.1:{self.port}/mjpeg2.php?camid=dead"
        t = time.time()
        with self.assertRaises(ValueError):
            _mjpeg_first_frame(url, timeout=1.0)
        elapsed = time.time() - t
        # read1 returns after each recv, so the 1s budget applies;
        # the old read(65536) would sit until the server's 10s EOF.
        self.assertLess(elapsed, 3.5,
                        f"dribbling stream held {elapsed:.1f}s — "
                        "read() fill-buffer regression?")


if __name__ == "__main__":
    unittest.main()
