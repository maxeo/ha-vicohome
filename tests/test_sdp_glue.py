"""Offline checks for the SDP glue (no Home Assistant needed)."""
import base64
import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "vicohome"
pkg = types.ModuleType("vicohome"); pkg.__path__ = [str(ROOT)]; sys.modules["vicohome"] = pkg
sys.modules["vicohome.api"] = types.SimpleNamespace(VicoApi=object)
sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
for name in ("vico_sdp", "signaling"):
    spec = importlib.util.spec_from_file_location(f"vicohome.{name}", ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec); sys.modules[f"vicohome.{name}"] = mod; spec.loader.exec_module(mod)
sig = sys.modules["vicohome.signaling"]; sdp = sys.modules["vicohome.vico_sdp"]

FP = ":".join(["AB"] * 32)
def section(kind, mid, direction):
    if kind == "application":
        return [f"m=application 9 UDP/DTLS/SCTP webrtc-datachannel", "c=IN IP4 0.0.0.0",
                f"a=ice-ufrag:uf", "a=ice-pwd:pw0123456789012345678901", f"a=fingerprint:sha-256 {FP}",
                "a=setup:actpass", f"a=mid:{mid}", "a=sctp-port:5000", "a=max-message-size:262144"]
    if kind == "audio":
        return ["m=audio 9 UDP/TLS/RTP/SAVPF 111 0 8", "c=IN IP4 0.0.0.0", "a=ice-ufrag:uf",
                "a=ice-pwd:pw0123456789012345678901", f"a=fingerprint:sha-256 {FP}", "a=setup:actpass",
                f"a=mid:{mid}", f"a={direction}", "a=rtcp-mux", "a=rtpmap:111 opus/48000/2",
                "a=fmtp:111 minptime=10;useinbandfec=1", "a=rtpmap:0 PCMU/8000", "a=rtpmap:8 PCMA/8000"]
    return ["m=video 9 UDP/TLS/RTP/SAVPF 96 97", "c=IN IP4 0.0.0.0", "a=ice-ufrag:uf",
            "a=ice-pwd:pw0123456789012345678901", f"a=fingerprint:sha-256 {FP}", "a=setup:actpass",
            f"a=mid:{mid}", f"a={direction}", "a=rtcp-mux", "a=rtpmap:96 H264/90000",
            "a=rtcp-fb:96 goog-remb", "a=rtcp-fb:96 transport-cc", "a=rtcp-fb:96 ccm fir",
            "a=rtcp-fb:96 nack", "a=rtcp-fb:96 nack pli",
            "a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
            "a=rtpmap:97 rtx/90000", "a=fmtp:97 apt=96"]

def offer(order):
    head = ["v=0", "o=- 4611731400430051336 2 IN IP4 127.0.0.1", "s=-", "t=0 0",
            "a=group:BUNDLE " + " ".join(str(i) for i in range(len(order))), "a=msid-semantic: WMS"]
    body = []
    for i, (k, d) in enumerate(order):
        body += section(k, i, d)
    return "\r\n".join(head + body) + "\r\n"

def camera_answer(compact):
    mid = {k: v for k, v in (("a", compact["a"][0]), ("v", compact["v"][0][0]), ("dc", compact["dc"][0]))}
    c = {"t": "answer", "sid": "123", "sv": 2, "ice": ["cu", "cpwd012345678901234567"],
         "fp": base64.b64encode(bytes(32)).decode(),
         "a": [mid["a"], 1, [1111, "camcname"], "Stream-1", "trackA"], "acp": [[0, 2]],
         "v": [[mid["v"], 3, [2222, 3333], "Stream-1", "trackV", "camcname"]], "vcp": [[96, 1, 3, 97]],
         "dc": [mid["dc"], 5000, 262144]}
    return sdp.decompress_answer(c)

for order in ([("audio", "recvonly"), ("video", "recvonly"), ("application", None)],
              [("application", None), ("audio", "recvonly"), ("video", "recvonly")]):
    o = offer(order)
    compact = sig.build_camera_offer(o)
    assert compact["a"][1] == 1 and len(compact["a"]) == 3, compact["a"]
    ans = sig.adapt_answer(camera_answer(compact), o)
    kinds = [ln.split()[0][2:] for ln in ans.split("\r\n") if ln.startswith("m=")]
    assert kinds == [k for k, _ in order], (kinds, order)
    mids = [ln[6:] for ln in ans.split("\r\n") if ln.startswith("a=mid:")]
    assert mids == [str(i) for i in range(len(order))], mids
    assert f"a=group:BUNDLE {' '.join(mids)}" in ans
    audio = ans.split("m=audio")[1].split("m=")[0]
    assert "a=sendonly" in audio and "a=sendrecv" not in audio, audio
    print("ok", [k for k, _ in order])
