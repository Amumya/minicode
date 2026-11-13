"""一份带多行字典的配置。

改这个文件的时候要注意：字典里有中文和引号，用 shell 重写整段很容易把转义搞错。
"""

DEFAULT = {
    "timeout": 30,
    "retries": 2,
    "endpoint": "https://example.com/api",
    "headers": {"X-Trace": "on", "X-Note": "如果超时就把 timeout 调大"},
}

DESCRIPTION = "默认配置，改之前先看清楚每个键是干什么的"
