# ChatGPT 网页端人工翻译桥接

状态：v0.4.0 候选功能，尚未发布。

## 为什么不是自动网页 Provider

OpenAI 官方文档把 [ChatGPT 网页端](https://learn.chatgpt.com/docs/web)描述为登录后
选择 Chat 或 Work、发送消息并人工复核结果的交互界面；程序化构建入口则是使用
API Key 的 [OpenAI API](https://developers.openai.com/api/docs)。PaperLocale 因此不
登录、抓取、逆向或自动点击 `chatgpt.com`，也不读取 Cookie、浏览器存储或账户凭据。

本功能采用可审计的人工文件桥接：PaperLocale 负责确定性分批和验证，用户只负责把
提示粘贴到普通 `Chat`，再把 JSON 回复保存回运行目录。模型调用发生在用户选择的
ChatGPT 网页会话中，而不是 `codex-local`。但具体模型、消息上限和用量仍取决于账户、
计划、地区和当时界面；项目不能承诺“免费”“不限量”或“必然不计入某项额度”。官方
说明还明确指出 ChatGPT Work 与 Codex 共享使用限额，因此本流程要求普通 `Chat`，
不把 Work 当作额度旁路。

## 工作流

先按正常流程完成收集、参考文献确认和必要的人工透传：

```bash
paperlocale init-run paper.pdf --run-dir runs/paper
paperlocale collect --run-dir runs/paper
paperlocale reference-review --run-dir runs/paper
paperlocale confirm-references \
  --run-dir runs/paper \
  --confirmed-by "your name"
```

若 `segment_safety_review.jsonl` 要求透传碎词、不可见短文本、纯公式或作者串，逐条
核对后使用 `confirm-passthrough`。不要通过削弱全局中文门禁绕过这一步。

导出网页批次：

```bash
paperlocale chatgpt-web-export \
  --run-dir runs/paper \
  --domain atmospheric-science \
  --reference-policy preserve \
  --max-segments 20 \
  --max-characters 12000
```

命令生成：

- `chatgpt_web/batch_manifest.json`：绑定全部输入身份和分批边界；
- `chatgpt_web/prompts/batch-NNN.md`：可直接粘贴的完整提示；
- `chatgpt_web/responses/batch-NNN.json`：网页回复的预期保存位置。

对每个提示执行以下人工步骤：

1. 登录 `chatgpt.com`，选择普通 `Chat`，不要选择 Codex 或 ChatGPT Work；
2. 新建独立聊天并粘贴一个完整 `batch-NNN.md`；
3. 核对回复只有 `batch_id`、`batch_sha256`、`translations` 三个顶层字段；
4. 去掉 Markdown 代码围栏，把完整 JSON 保存到清单指定的响应路径；
5. 按网页模型选择器原样记下模型标签，不能事后猜测底层模型版本。

导入回复：

```bash
paperlocale chatgpt-web-import \
  --run-dir runs/paper \
  --domain atmospheric-science \
  --reference-policy preserve \
  --model-label "网页上实际显示的模型标签"
```

导入会拒绝错误批次哈希、缺失/重复/额外 ID、公式或富文本占位符丢失、数字和单位
丢失、固定术语错误等问题。同批合格译文仍会原子保存；修订失败回复后再次导入，只
处理尚未通过的片段。每次参与导入的网页回复会复制到 `chatgpt_web/imports/`，并在
`import_history.jsonl` 中记录哈希、模型标签和结果。

全部译文闭合后继续唯一生产路径：

```bash
paperlocale validate --run-dir runs/paper --domain atmospheric-science
paperlocale render --run-dir runs/paper
paperlocale qa --run-dir runs/paper
paperlocale accept --run-dir runs/paper --reviewed-by "your name"
```

机器 QA 通过不代表视觉完全正确。仍须逐页检查中文断行、公式、表格、图片、图注、
多栏顺序、页眉页脚和参考文献版面。

## 审计边界

- 记录：源 PDF/片段/领域包/人工映射哈希、批次提示哈希、网页回复快照哈希、人工
  可见模型标签、导入成功或失败记录；
- 不记录：ChatGPT 账号、Cookie、会话令牌、密码、API Key；
- 不证明：网页显示标签对应的不可见后端快照、账户计费或配额规则、翻译语义已经由
  独立领域专家确认；
- 不分发：受版权限制的源论文、完整译本或私人 ChatGPT 会话链接。
