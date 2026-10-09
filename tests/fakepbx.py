"""Mini registrar SIP (UDP) pour les tests : authentification Digest + MWI.

Usage : python3 fakepbx.py PORT EXTENSION PASSWORD
"""
import hashlib
import random
import socket
import sys
import time

PORT = int(sys.argv[1])
EXT, PASSWORD = sys.argv[2], sys.argv[3]
REALM, NONCE = "emaktest", "abcdef123456"
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("127.0.0.1", PORT))
contacts = {}


def log(*a):
    print("[pbx]", *a, flush=True)


def parse(data):
    text = data.decode("utf-8", "replace")
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    start = lines[0]
    headers = []
    for l in lines[1:]:
        k, _, v = l.partition(":")
        headers.append((k.strip(), v.strip()))
    return start, headers, body


def hget(headers, name, short=None):
    for k, v in headers:
        if k.lower() == name.lower() or (short and k.lower() == short):
            return v
    return None


def hall(headers, name, short=None):
    return [v for k, v in headers if k.lower() == name.lower() or (short and k.lower() == short)]


def respond(addr, headers, code, reason, extra=(), body=""):
    out = ["SIP/2.0 %d %s" % (code, reason)]
    for v in hall(headers, "Via", "v"):
        out.append("Via: " + v)
    out.append("From: " + hget(headers, "From", "f"))
    to = hget(headers, "To", "t")
    if ";tag=" not in to:
        to += ";tag=pbx%d" % random.randint(1000, 9999)
    out.append("To: " + to)
    out.append("Call-ID: " + hget(headers, "Call-ID", "i"))
    out.append("CSeq: " + hget(headers, "CSeq"))
    out.extend(extra)
    out.append("Content-Length: %d" % len(body.encode()))
    sock.sendto(("\r\n".join(out) + "\r\n\r\n" + body).encode(), addr)
    return to


def digest_ok(auth, method):
    params = {}
    for part in auth[len("Digest "):].split(","):
        k, _, v = part.strip().partition("=")
        params[k] = v.strip('"')
    ha1 = hashlib.md5(("%s:%s:%s" % (params.get("username"), REALM, PASSWORD)).encode()).hexdigest()
    ha2 = hashlib.md5(("%s:%s" % (method, params.get("uri"))).encode()).hexdigest()
    if params.get("qop"):
        resp = hashlib.md5(("%s:%s:%s:%s:%s:%s" % (ha1, params["nonce"], params["nc"], params["cnonce"],
                                                    params["qop"], ha2)).encode()).hexdigest()
    else:
        resp = hashlib.md5(("%s:%s:%s" % (ha1, params["nonce"], ha2)).encode()).hexdigest()
    return params.get("username") == EXT and resp == params.get("response")


def send_notify(addr, sub_headers, to_with_tag, contact_uri, body):
    via = "SIP/2.0/UDP 127.0.0.1:%d;branch=z9hG4bK%d;rport" % (PORT, random.randint(1, 10 ** 9))
    frm = to_with_tag  # the subscription's To (with our tag) becomes our From
    to = hget(sub_headers, "From", "f")
    msg = [
        "NOTIFY %s SIP/2.0" % contact_uri,
        "Via: " + via,
        "Max-Forwards: 70",
        "From: " + frm,
        "To: " + to,
        "Call-ID: " + hget(sub_headers, "Call-ID", "i"),
        "CSeq: 1 NOTIFY",
        "Contact: <sip:pbx@127.0.0.1:%d>" % PORT,
        "Event: message-summary",
        "Subscription-State: active;expires=3600",
        "Content-Type: application/simple-message-summary",
        "Content-Length: %d" % len(body.encode()),
    ]
    sock.sendto(("\r\n".join(msg) + "\r\n\r\n" + body).encode(), addr)
    log("NOTIFY sent")


while True:
    data, addr = sock.recvfrom(65535)
    if data.strip() == b"":
        continue
    start, headers, body = parse(data)
    if start.startswith("SIP/2.0"):
        continue
    method = start.split()[0]
    if method == "REGISTER":
        auth = hget(headers, "Authorization")
        if not auth:
            respond(addr, headers, 401, "Unauthorized",
                    ['WWW-Authenticate: Digest realm="%s", nonce="%s", algorithm=MD5' % (REALM, NONCE)])
            log("REGISTER challenge")
        elif digest_ok(auth, "REGISTER"):
            contact = hget(headers, "Contact", "m") or ""
            expires = hget(headers, "Expires") or (contact.split("expires=")[1].split(";")[0] if "expires=" in contact else "60")
            respond(addr, headers, 200, "OK", ["Contact: %s;expires=%s" % (contact.split(";expires")[0], expires), "Expires: %s" % expires])
            contacts[EXT] = (contact, addr)
            log("REGISTER ok contact=%s expires=%s" % (contact, expires))
        else:
            respond(addr, headers, 403, "Forbidden")
            log("REGISTER forbidden")
    elif method == "SUBSCRIBE":
        to_tag = respond(addr, headers, 200, "OK", ["Expires: 3600", "Contact: <sip:pbx@127.0.0.1:%d>" % PORT])
        contact = hget(headers, "Contact", "m") or ""
        uri = contact.split("<")[1].split(">")[0] if "<" in contact else contact
        time.sleep(0.2)
        send_notify(addr, headers, to_tag, uri, "Messages-Waiting: yes\r\nVoice-Message: 2/1 (0/0)\r\n")
        log("SUBSCRIBE ok")
    elif method in ("OPTIONS", "PUBLISH"):
        respond(addr, headers, 200, "OK")
    elif method != "ACK":
        respond(addr, headers, 501, "Not Implemented")
        log("unhandled", method)
