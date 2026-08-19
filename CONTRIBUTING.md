# Contributing

欢迎贡献解析兼容性、Provider、领域包、测试和文档。

参与 Issue、PR、评审或其他项目空间即表示同意遵守
[PaperLocale Code of Conduct](CODE_OF_CONDUCT.md)。行为问题请按其中说明私密
报告，不要在公开 Issue 中披露个人敏感信息。

提交前请：

1. 不添加受版权限制的论文或译文；
2. 不添加真实密钥、Token 或本机登录文件；
3. 为科学信息门禁变化增加回归测试；
4. 为领域术语注明含义和使用边界；
5. 执行 `python -m pip install -e ".[test]"` 后运行
   `python -m unittest discover -s tests -v`。

复核 Provider 评估时，必须逐条填写对应工作表的接受/需修改结论和专业理由。参考译文与候选译文不逐字一致不等于错误；不得用字符串相似度代替术语、程度词、相关/因果和自然度判断。

涉及 BabelDOC/PDFMathTranslate 的解析、渲染或许可证边界时，请先开 Issue 讨论。

版面桥接变更还必须执行：

```bash
python -m pip install -e ".[layout,test]"
python scripts/layout_smoke.py --output tmp/layout-smoke-你的编号
```

脚本通过后仍需查看它输出的逐页对照图；机器 QA 不等于人工视觉验收。
