# 访问方式与本地演示账号

本地演示入口：http://127.0.0.1:8000/

线上体验地址与评审凭据通过比赛指定渠道单独提供，不随公开材料分发。

线上评审凭据通过比赛指定渠道单独提供，不包含在源码、报告或运行包中。以下账号仅适用于本地下载运行包及一次性测试副本，不能作为线上登录凭据。

| 账号 | 角色 | 密码 | 可用环境 |
|---|---|---|---|
| competition-viewer | viewer | IssuerLocal-Viewer-2026! | 本地演示 |
| competition-analyst | analyst | IssuerLocal-Analyst-2026! | 本地演示 |
| competition-analyst2 | analyst | IssuerLocal-Analyst2-2026! | 本地演示 |
| competition-reviewer | reviewer | IssuerLocal-Reviewer-2026! | 本地演示 |
| competition-admin | admin | IssuerLocal-Admin-2026! | 本地演示 |

以上为公开的虚构演示身份。部署到公网前，请修改或停用全部演示账号，配置独立签名密钥、HTTPS 和访问控制。分析、复核与管理员写操作只在自己的演示数据库执行。

运行包保留处理后的分析数据，清除真实身份及人工操作记录。公开源码与线上评审账号采用独立凭据管理，压力测试仅针对独立数据库副本。
