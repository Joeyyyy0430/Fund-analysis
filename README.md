# FundOS

一个基于 `Streamlit` 的基金持仓与交易复盘工具。

## 公开仓库说明

这个公开版本不包含任何真实交易记录、账本数据库、PDF 对账单或本地调试产物。
交易数据会在你本地导入 PDF 后生成到 `fund_data/` 中，不会随仓库提交。

## 功能

- 基金持仓总览与分类分布
- 交易日复盘
- 基金详情页走势与信号查看
- 支持从支付宝基金交易 PDF 增量同步数据

## 安装

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 启动应用

```bash
streamlit run fund_app.py
```

## 导入交易记录

方式 1：在页面左侧上传交易记录 PDF。

方式 2：命令行同步：

```bash
python sync_trades.py /path/to/statement.pdf
```

如果需要用整份账单替换旧 PDF 记录：

```bash
python sync_trades.py /path/to/full_statement.pdf --snapshot
```

## 数据目录

- `fund_data/transactions.csv`：本地导出的兼容 CSV
- `fund_data/trades.db`：本地 SQLite 账本

这两个文件都会在你本地导入后自动生成，并被 `.gitignore` 忽略。
