---
triggers:
    - provider: github
      event: issue_mentioned
      filter:
        labels:
            - factory:linking-test
        mentioned:
            - warp-local-factory
        repos:
            - warpdotdev/oz-for-oss
    - provider: github
      event: pull_request_mentioned
      filter:
        labels:
            - factory:linking-test
        mentioned:
            - warp-local-factory
        repos:
            - warpdotdev/oz-for-oss
    - provider: github
      event: issue_assigned
      filter:
        assignees:
            - warp-local-factory
        labels:
            - factory:linking-test
        repos:
            - warpdotdev/oz-for-oss
    - provider: github
      event: pull_request_assigned
      filter:
        assignees:
            - warp-local-factory
        labels:
            - factory:linking-test
        repos:
            - warpdotdev/oz-for-oss
---

