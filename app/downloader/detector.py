from urllib.parse import urlparse


PLATFORMS = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "fb.watch": "Facebook",
    "reddit.com": "Reddit",
    "x.com": "X",
    "twitter.com": "X",
    "tiktok.com": "TikTok",
    "pinterest.com": "Pinterest",
    "threads.net": "Threads",
}


def detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]

    for domain, name in PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return "Unknown"
