---
name: hams-aws-session-touch-interval
description: "On hams.com/hams_open, the AWS CLI session (profile \"hams-com\") must be used at least once every 12 hours or it expires and requires Bruce's own interactive browser-based re-authentication."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 58453db6-5ee8-4667-a749-b2d8dad1938a
  modified: 2026-08-26T17:35:29.165Z
---

On hams.com/hams_open, Claude has real, working AWS CLI access via the `hams-com` AWS profile (`~/.aws/config`, account `469244221480`, region `us-east-1`) — this is not a permission restriction Claude imposes on itself; commands actually execute against real AWS infrastructure when the session is valid. However, the underlying AWS CLI `login` session (a console-credential session, not a static access key) expires if left unused, and Bruce has stated the session must be touched (any AWS CLI call under that profile) at least once every 12 hours or it goes stale.

Once the session expires, `aws --profile hams-com <command>` fails with "Your session has expired. Please reauthenticate using 'aws login'." Re-authentication is `aws login --remote` (the `--remote` flag is required in this headless/SSH environment, since it prints a URL and prompts for a pasted authorization code instead of opening a local browser) — but this step is inherently interactive and requires a human with a browser to complete the AWS Console login and paste back the authorization code. Claude cannot complete this re-authentication step itself; only Bruce can when the session has actually expired.

The practical implication: during any long-running or overnight autonomous session on this project, Claude should periodically run a cheap, harmless AWS CLI call (e.g. `aws --profile hams-com sts get-caller-identity`) at least once every 12 hours to keep the session alive, rather than letting it sit idle and expire, which would otherwise force an interruption to get Bruce to manually re-authenticate before AWS-dependent work (e.g. S3 backup storage, SES credential work) can continue.
