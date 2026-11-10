# 运行与资料分离

## 依赖和目录

公共仓库维护索引、查询、证据适配和规则元数据；实际原文、全量 manifest、既有登记、上游运行包与数据库由运行环境持有。外部算核的身份固定在 [runtime-dependency.json](../rules/runtime-dependency.json)，需要原有授权才能获得。

完整分析使用 Python 3.13 固定镜像及只读隔离容器。已有服务器发行目录的布局如下；这不是本仓库内要提交的目录：

```text
release/
  app/                 已锁定的应用和私有 evidence_seed.json
  runtime/             上游封版算核及 installation.json
  sources/             本地原件或固定页图
  adoption-proofs/      采用凭证（如该版本需要）
  release-lock.json     应用、资料、种子及数据库身份
  catalog.sqlite       指向同版本数据库
```

`src/server_cli.py` 是现有管理员安装文件的原样副本，默认使用 `/opt/bazi-knowledge/current` 和 `/var/lib/bazi-knowledge`。复制仓库不会修改部署或安装该 CLI；已有环境继续使用其既有入口。

## 1. 验证规则并恢复私有种子

```bash
python3 -B scripts/validate_rules.py
python3 -B scripts/materialize_seed.py \
  --private-seed "$GX_PRIVATE_SEED" \
  --output .local/evidence_seed.json
```

工具核对私有文件完整字节 SHA、公开投影逐字段一致、引文哈希，然后以 `0600` 写入新文件；已存在的输出不覆盖。它不验证原件 PDF 本身，原件完整性由发行锁和实际运行验证。不要把 `.local` 或恢复后的文件加入 Git。

种子固定 SHA：`34b6ce379dd78a5d482fa81bf267f6eb47e4095e090f2abf572c600d373e2594`。此工具服务于当前封版；资料和规则变更应另建版本并重新验证，不能绕过身份校验。

## 2. 构建或增量更新索引

首次构建时，`GX_ARCHIVE` 指向带 `manifest.sqlite` 的授权原档案目录；`GX_UPSTREAM` 指向固定提交的上游研究登记 checkout：

```bash
python3 -B src/catalog_builder.py \
  --archive "$GX_ARCHIVE" --repo "$GX_UPSTREAM" \
  --evidence-seed .local/evidence_seed.json \
  --output .local/catalog-v2.sqlite
```

默认读取该交付版已锁定的 95 方法与 138 路由登记，不能把另一批资料静默当成本项目封版。原始资料只读；输出必须位于资料及上游目录之外。

已有索引时复用全部资产、路径和注册表，只替换核准的证据表：

```bash
python3 -B scripts/expand_catalog.py \
  --base-db "$GX_BASE_DB" --base-sha256 "$GX_BASE_DB_SHA256" \
  --seed .local/evidence_seed.json --app src \
  --output .local/catalog-next.sqlite
```

`GX_BASE_DB_SHA256` 应取已保存的可信版本记录。工具拒绝覆盖文件并检查输入身份、外键及 SQLite 完整性。数据库构建时间会影响文件哈希；重建结果不能直接替换旧发行锁所绑定的文件。

检索和检查规则：

```bash
python3 -B src/catalog_queries.py --db .local/catalog-v2.sqlite meta
python3 -B src/catalog_queries.py --db .local/catalog-v2.sqlite search '有印而透官'
python3 -B src/catalog_queries.py --db .local/catalog-v2.sqlite rule 'B-PRINT§1'
```

搜索只用于发现材料；查询层明确返回 `establishes_rule_support=false`。最终引用只使用已批准的显式关联。

## 3. 在已有发行环境分析

推荐已有服务器的审计入口：

```bash
bazi-evidence analyze examples/B13_wu_relations_jiaji_candidate.json
bazi-evidence stats
```

`src/server_cli.py` 包裹固定 Docker 调用，记录请求／响应、SHA、版本、耗时和请求编号。直接调用 `pipeline.py` 的测试容器不等同于该审计入口。

若需独立检查已经封好的发行版本，令 `GX_RELEASE` 为该版本的绝对目录、`GX_CATALOG` 为它配对的数据库绝对路径；镜像须预先准备好。以下命令不拉取资料：

```bash
GX_IMAGE="$(python3 -c 'import json; print(json.load(open("rules/runtime-dependency.json"))["container_image"])')"
docker run --rm -i --pull never --network none --ipc none --read-only \
  --user 65534:65534 --cpus 1 --memory 1g --memory-swap 1g \
  --pids-limit 64 --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=96m \
  --mount "type=bind,src=$GX_RELEASE,dst=/release,readonly" \
  --mount "type=bind,src=$GX_CATALOG,dst=/catalog.sqlite,readonly" \
  "$GX_IMAGE" python3 -B /release/app/pipeline.py \
  --release /release --db /catalog.sqlite analyze - \
  < examples/B13_wu_relations_jiaji_candidate.json
```

输入沿用原生 `core-yongshen-input-0.1`：本命四柱，逐时点的运／年／月／日／时五柱及带时区的时点。示例均为构造测试，未经过真实日期排盘认证。最多 32 个时点、128 KiB 输入；缺项、重复 JSON 键和超长请求会被拒绝。

## 4. 维护来源对应规则

每个公开片段保留 `passage_id`、`source_id`、页码／行号和引文哈希；每个规则保留原规则 ID、存在的原声明条件、采用凭证定位与适用限制；每个绑定保留精确 method、可用状态和必要原生谓词。

完整资料中的本轮封版可通过以下命令重新生成公开投影（输出必须是新目录）：

```bash
python3 -B scripts/export_rules.py \
  --seed "$GX_PRIVATE_SEED" --inventory "$GX_PRIVATE_INVENTORY" \
  --cases "$GX_PRIVATE_CASES" --output .local/public-projection
```

发布前执行合成测试、规则校验和 `python3 -B scripts/check_publication.py --staged-only`。后续更换实际算核、规则或来源，需要在授权运行环境逐方法重新跑集成验证；公共单元测试不能代替该环节。
