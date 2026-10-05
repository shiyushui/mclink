# 测试用一次性证书

`TEST-ONLY.key` / `TEST-ONLY.crt` 是一对**故意公开的、用完即弃的自签证书**，
只给 `test/tls_test.py` 用：当本机没有 openssl（纯 Windows 上常见）时，
测试用它们来起一个 TLS 服务端，验证"证书指纹校验"这条链路。

**它们不是任何真实服务器的证书，也不对应任何真实密钥。** 公开在这里是有意为之。

> 如果你把本仓库 fork 出去，看到 GitHub 的 secret scanning 提示这两个文件
> 含私钥 —— 那是预期行为。它们是测试素材，不需要撤销，也不需要轮换。
> 真的想要一对新的，随便生成即可：
>
> ```bash
> openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
>   -keyout TEST-ONLY.key -out TEST-ONLY.crt -subj "/CN=localhost"
> ```
