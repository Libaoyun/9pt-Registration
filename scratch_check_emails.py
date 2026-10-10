import urllib.request
import re

req = urllib.request.Request(
    'https://icloud-api.top/s/faHDMh9uBG1VRXzV0Hfgdo5GvNEzmGJy/lumbar_mailing_1c@icloud.com',
    headers={'User-Agent': 'Mozilla/5.0'}
)
html = urllib.request.urlopen(req).read().decode('utf-8')
cards = html.split('<div class="card">')
print(f"Total cards: {len(cards) - 1}")
for i, c in enumerate(cards[1:], 1):
    m_su = re.search(r'<div class="su">(.*?)</div>', c)
    m_dt = re.search(r'<div class="dt">(.*?)</div>', c)
    print(f"Card {i}: {m_dt.group(1) if m_dt else ''} | {m_su.group(1) if m_su else ''}")
