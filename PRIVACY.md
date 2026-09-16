# Privacy Policy — Miscellaneous Ken Studio

_Last updated: 16 September 2026_

Miscellaneous Ken Studio ("the app") is a personal video-production tool. It runs
locally on the developer's own Mac and publishes to the developer's own YouTube
channel. It has a single user: its developer.

## Who operates the app

Nana Dankwa Oduro-Wadie — dankwawadie@gmail.com

## What the app accesses

The app requests one Google OAuth scope:

- `https://www.googleapis.com/auth/youtube.upload` — permission to upload videos
  to the YouTube channel of the signed-in account.

This scope allows uploading only. The app does not read your subscriptions,
watch history, comments, private videos, or account details, and it cannot
delete or modify existing videos.

## What the app stores

Everything the app stores is kept locally on the developer's own computer:

- video files it has rendered, and the source clips used to make them
- draft metadata: titles, descriptions, on-screen text, and publish settings
- an OAuth token used to upload to YouTube

No database, server, or cloud account belonging to the developer holds this
information. Nothing is transmitted to the developer from any other person's
device, because no one else uses the app.

## What the app sends elsewhere

- **YouTube (Google).** Finished videos, with their titles and descriptions, are
  uploaded to the developer's own channel via the YouTube Data API.
- **AI providers.** To choose stories and write on-screen text, the app may send
  a still frame, a short audio excerpt, or article text to Google Gemini,
  Anthropic, or OpenAI, depending on configuration. This is used for inference
  only. The app does not send this material for model training.

## What the app does not do

- It does not collect personal information from anyone.
- It does not use analytics, advertising, or tracking of any kind.
- It does not sell, rent, or share data with anyone for marketing.
- It has no other users whose data could be collected.

## Google user data

Data obtained through Google APIs is used solely to upload videos to the
developer's own channel. It is not transferred to any third party except as
described above, and its use adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Retention and deletion

Rendered videos and source files are deleted from the computer when the
corresponding draft is deleted in the app. The OAuth token can be revoked at any
time from the Google Account permissions page at
https://myaccount.google.com/permissions, which immediately ends the app's
access.

## Changes

If this policy changes, the revision date above will be updated.

## Contact

Questions about this policy: dankwawadie@gmail.com
