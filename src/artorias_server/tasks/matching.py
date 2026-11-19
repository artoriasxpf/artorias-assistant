import re

def terms(text):
    return set(re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]{2}', text.lower()))
