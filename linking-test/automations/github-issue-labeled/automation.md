---
triggers:
    - provider: github
      event: issue_labeled
      filter:
        labels:
            - factory:linking-test
        repos:
            - warpdotdev/oz-for-oss
---

