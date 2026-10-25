# React + TypeScript + Vite

This template provides a minimal setup to get React working in Vite with HMR and some Oxlint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the Oxlint configuration

If you are developing a production application, we recommend enabling type-aware lint rules by installing `oxlint-tsgolint` and editing `.oxlintrc.json`:

```json
{
  "$schema": "./node_modules/oxlint/configuration_schema.json",
  "plugins": ["react", "typescript", "oxc"],
  "options": {
    "typeAware": true
  },
  "rules": {
    "react/rules-of-hooks": "error",
    "react/only-export-components": ["warn", { "allowConstantExport": true }]
  }
}
```

See the [Oxlint rules documentation](https://oxc.rs/docs/guide/usage/linter/rules) for the full list of rules and categories.

## Story 配置

管理端新增「Story 配置」，API由 `https://story.lazylady.cc/api/admin` 提供。配置页通过 `/session` 实际验证管理员密码并获取15分钟专用会话，随后用该会话访问 `/config`；不使用共享SSO JWT作为Story配置凭证，不把Story会话或密码写入浏览器存储。服务器配置持久化在Story专属运行目录，管理端仅发布静态界面。

契约、验收和部署约束见相邻项目 `../little-story-press/omx_wiki/specs/admin-configuration.md`；更新本页前请保留用户/额度管理和其他平台服务。

## 每日额度与今日剩余

用户管理分别显示、编辑以下两项：

- **每日额度**：长期配置，从下一次 UTC 00:00（北京时间08:00）日切起使用；支持0以及恢复应用默认值。更改时不返还今天已使用的次数。
- **今日剩余**：将当前可用余额设置为指定数值，立即生效，并非在原余额上叠加；次日日切按每日配置恢复。

已接入中央扣费的应用为宝贝档案、C学习、志愿规划、绘本，以及独立桥接的棋牌、厨房。2026-10-24新增小C英语：其专用预留、提交、退款继续由英语扩展处理，跨天额度改为读取同一长期策略；管理端支持修改其每日额度和今日剩余。

用户列表优先显示管理员备注、英语档案昵称，未填写昵称的微信账号显示账号编号。可按昵称、备注、编号或登录标识搜索；备注不修改登录名和用户权限。英语昵称适配器只读现有英语状态文件，不返回微信openid、会话或学习明细，昵称不用于合并身份。

部署源码及回归测试位于 `deploy/`、`tests/`。中央服务为独立进程，不替换既有 SSO/英语源码；棋牌、厨房桥接以线上当前源码为基线，仅调整额度读取与日切。策略保存在中央数据库的独立表 `user_app_quota_policies`，管理员写入记录在 `quota_admin_audit`；不将当日余额回填为长期配置。

运行验证：

```sh
python3 -m unittest discover -s tests -v
npm run lint
npm run build
```

完整验收和发布约束见 `omx_wiki/specs/daily-user-quotas.md`。生产回滚仅回退代码和路由，不能用旧共享数据库覆盖新用户数据。
