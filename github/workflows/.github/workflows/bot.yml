name: paper-bot
on:
  schedule:
    - cron: "5 * * * *"
  workflow_dispatch:
permissions:
  contents: write
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python bot.py live
      - run: |
          git config user.name "paper-bot"
          git config user.email "paper-bot@users.noreply.github.com"
          git add -A
          git diff --cached --quiet || git commit -m "update paper results"
          git push
