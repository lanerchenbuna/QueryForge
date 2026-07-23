# QueryForge GitHub 发布清单

这份清单用于把当前本地目录首次发布到 GitHub。项目数据应完整提交，
本地密钥、虚拟环境、缓存和运行产物不得提交。

## 1. 选择许可证

公开仓库不等于开源。正式公开前先选择并添加根目录 `LICENSE`：

- MIT：简洁、宽松；
- Apache-2.0：宽松，并包含明确的专利授权；
- 暂不授权：仓库保持 private，不添加开源许可证。

可以用 `gh repo license list` 查看 GitHub CLI 支持的许可证，或使用
[Choose a License](https://choosealicense.com/) 生成完整文本。不要自行删改许可证正文。

## 2. 重建干净开发环境

```bash
cd QueryForge

python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

cp .env.example .env
# 仅在本地 .env 中填写密钥；不要提交它。
```

运行发布前检查：

```bash
make check
make semantic-check
make package
python -m twine check dist/*
```

这些命令会重新生成缓存、报告和构建目录，但它们已经被 `.gitignore`
排除，不会进入仓库。

## 3. 初始化 Git

当前目录尚未初始化为 Git 仓库：

```bash
git init -b main
git add .
git status --short
```

提交前人工确认：

- 输出中没有 `.env`、`.venv/`、`.queryforge/`、日志或缓存；
- `sample_data/anime_streaming/anime_streaming.sqlite` 在暂存区；
- `sample_data/anime_streaming/tables/` 下 15 份 CSV 都在暂存区；
- `semantic_model.yml` 和 `semantic_baseline.json` 都在暂存区；
- 根目录已经有你选择的 `LICENSE`。

然后创建首个提交：

```bash
git commit -m "feat: publish QueryForge"
```

## 4. 创建并推送 GitHub 仓库

推荐使用 GitHub CLI：

```bash
gh auth login
gh repo create QueryForge \
  --public \
  --source=. \
  --remote=origin \
  --push
```

如果发布到组织，使用 `OWNER/QueryForge` 作为仓库名。

也可以先在 GitHub 网页创建空仓库。不要在网页端额外初始化 README、
`.gitignore` 或许可证，然后执行：

```bash
git remote add origin git@github.com:YOUR_GITHUB_USER/QueryForge.git
git remote -v
git push -u origin main
```

GitHub 官方说明：
[添加本地代码到 GitHub](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)。

## 5. 等待首轮 Actions

打开仓库的 **Actions** 页面，确认：

- `quality` 在 Python 3.11 和 3.12 上通过；
- `package` 构建和元数据检查通过；
- `weekly-semantic-layer-check` 可以手动触发并通过。

也可以用 CLI 查看：

```bash
gh run list --workflow quality.yml --limit 5
gh run list --workflow semantic-weekly.yml --limit 5
```

## 6. 配置主分支保护

首轮 Actions 成功后，在 **Settings → Rules → Rulesets**（或 Branches）
为 `main` 添加规则：

- Require a pull request before merging；
- Require status checks to pass；
- 选择两个 `offline-acceptance` 矩阵检查和 `package`；
- Require conversation resolution；
- 禁止 force push 和删除 `main`。

单人维护时可以暂不强制其他人审批，但不应关闭状态检查。

GitHub 只允许选择近期成功运行过的状态检查，所以应先完成首次推送和
Actions，再配置 required checks。

## 7. 完善仓库展示与安全

在仓库首页填写：

- Description：`Governed AI analytics and NL2SQL with a mandatory semantic layer`
- Topics：`ai`、`analytics`、`data-agent`、`nl2sql`、`semantic-layer`、`sqlite`

在 **Settings → General → Social preview** 上传 PNG/JPG/GIF。GitHub 推荐
1280×640，并要求小于 1 MB。README 内的 SVG 和 Mermaid 会直接随代码展示。

在 **Settings → Security → Advanced Security** 启用：

- Dependabot alerts；
- Dependabot security updates；
- Private vulnerability reporting。

## 8. 创建首个版本

主分支检查全部通过后：

```bash
git tag -a v0.1.0 -m "QueryForge v0.1.0"
git push origin v0.1.0
gh release create v0.1.0 --generate-notes
```

发布完成后，从一个新的临时目录克隆仓库并重新执行安装与 `make check`，
这是最可靠的“陌生机器可复现”验证。
