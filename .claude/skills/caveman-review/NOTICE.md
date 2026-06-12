# Vendored skill：caveman-review

這個 skill **不是本專案原創**，是 vendor 進來的第三方 skill。

- **來源**：[JuliusBrussee/caveman](https://github.com/JuliusBrussee/caveman)（71.8k★）
- **授權**：MIT（見同目錄 `LICENSE`，著作權 © 2026 Julius Brussee）
- **為何 vendor 而不是 install.sh**：原專案用 `curl | bash` 安裝到使用者家目錄；
  在 GitHub Actions（`claude-code-action`）裡要載入 repo 內 skill，必須把 `SKILL.md`
  放進 `.claude/skills/<name>/` 並 commit、釘版本，CI checkout 後才找得到、可審、可重現。
- **怎麼用**：第一道 per-PR review（`.github/workflows/caveman-review.yml`），
  prompt 傳 `/caveman-review`，疊上本 repo 專屬檢查項（見該 workflow）。
- **更新策略**：手動同步上游（原樣保留 `SKILL.md`），不就地改它的內容；
  本 repo 的差異化（組織記憶感知）放在第二道 leader review，不污染這個 vendored skill。
