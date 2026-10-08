from opencc import OpenCC

from lulu.ops.localization import hans


def text_for(value, locale):
    if locale == "zh-Hans":
        return hans(value)
    if locale == "zh-Hant":
        return OpenCC("s2twp").convert(value or "")
    # English image pixels can retain packaging only; captions come from English copy.
    return value or ""
