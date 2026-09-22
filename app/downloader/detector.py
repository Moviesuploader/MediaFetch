from urllib.parse import urlparse


PLATFORMS = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "youtube-nocookie.com": "YouTube",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "fb.watch": "Facebook",
    "reddit.com": "Reddit",
    "redd.it": "Reddit",
    "x.com": "X",
    "twitter.com": "X",
    "t.co": "X",
    "tiktok.com": "TikTok",
    "vm.tiktok.com": "TikTok",
    "pinterest.com": "Pinterest",
    "pin.it": "Pinterest",
    "threads.net": "Threads",
    "threads.com": "Threads",
}


def detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]

    for domain, name in PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return "Unknown"
