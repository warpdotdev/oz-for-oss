---
triggers:
    - provider: github
      event: pull_request_labeled
      filter:
        labels:
            - factory:linking-test
        repos:
            - warpdotdev/oz-for-oss
---
Enter the procedure at the review stage for the triggering pull request. Do not triage or start a new implementation. If the pull request already has an in-flight factory task, adopt it instead of opening parallel work.
