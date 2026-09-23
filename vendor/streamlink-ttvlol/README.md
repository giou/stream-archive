# Vendored streamlink-ttvlol plugin

`<tag>/twitch.py` is the streamlink plugin the image bakes in. The recorder
imports it into its own process, so it runs with the app's authority: the bot
token, the Twitch and Kick client secrets, the YouTube token, and the data
directory. The file is vendored, not downloaded during the build, for three
reasons:

- The build needs no network, so it is reproducible.
- The bytes that ship are the bytes a reviewer read in the pull request.
- The file sits in git, so `git log` and the image contents agree.

## Contents

The plugin file lives under `<tag>/twitch.py`. The Dockerfile names that
directory with `TTVLOL_PLUGIN_VERSION`, records the digest of the copied
file, and CI checks the vendored file against `TTVLOL_PLUGIN_SHA256`.

## Updating

`.github/workflows/ttvlol-bump.yml` runs every Monday. It resolves the newest
upstream release, and when the tag differs from the pin it opens a pull
request that adds the new file, updates both `Dockerfile` arguments, and puts
the upstream diff in the body.

Review that diff. The plugin runs with full secrets authority, so a change to
it is a change to the trust boundary.

To bump by hand:

```sh
TAG=<new tag>
curl -fsSL -o /tmp/twitch.py \
  "https://github.com/2bc4/streamlink-ttvlol/releases/download/${TAG}/twitch.py"
sha256sum /tmp/twitch.py
diff -u vendor/streamlink-ttvlol/8.3.0-20260701/twitch.py /tmp/twitch.py
mkdir -p "vendor/streamlink-ttvlol/${TAG}"
cp /tmp/twitch.py "vendor/streamlink-ttvlol/${TAG}/twitch.py"
# Then set both ARG lines in the Dockerfile to the tag and the digest.
```

## License

The plugin comes from <https://github.com/2bc4/streamlink-ttvlol> (BSD-2-Clause,
derived from Streamlink). The file is redistributed unchanged. The upstream
license text follows.

```
Copyright (c) 2011-2016, Christopher Rosell
Copyright (c) 2016-2024, Streamlink Team
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
