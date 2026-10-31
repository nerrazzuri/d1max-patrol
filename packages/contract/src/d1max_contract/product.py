"""产品名、产品版本(W57):客户看到的只有这一个版本号 —— 站点、机器人代理、手机 App、网页都显示它。

改版本:改这里的 ``PRODUCT_VERSION``,然后跑 ``tests/test_product_version.py``,它会列出还没跟着改的文件
(各 ``pyproject.toml``、``d1max_patrol.__version__``、``mobile/pubspec.yaml``、App 的 ``appVersion``、
``web/console/package.json``)。写法:``主.次.修``,候选版加 ``-rc.N``(App 的构建号另记在 pubspec 的 ``+`` 后面)。
库结构、契约、接口级别各有自己的号,不跟着它变。
"""

PRODUCT_NAME = "Nexus Warden"
PRODUCT_VERSION = "1.0.0-rc.1"
