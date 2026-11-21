# Mootdx transport compatibility attribution

The fixed MAC command layouts in transport_compat.py were reviewed from:
https://github.com/clong365/easy_tdx/tree/4820b4a0496899ece0b8ca4d7f4d66a5159da7f8

Relevant source files are src/easy_tdx/mac/commands/symbol_bar.py,
src/easy_tdx/mac/commands/symbol_quotes.py, src/easy_tdx/codec/mac_frame.py,
src/easy_tdx/codec/bitmap.py and src/easy_tdx/config.py. This project implements
only bounded raw 5m/60m bars and one-stock quotes. It preserves full response
frames, rejects truncated payloads instead of silently truncating count, and
runs through the installed Mootdx session. It does not install or execute
remote easy_tdx source.

## MIT License

Copyright (c) 2025-present easy-tdx contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Upstream credits pytdx (Copyright (c) rainx and contributors) and xmtdx
(Copyright (c) minionszyw and contributors), both originally MIT licensed.
The installed Mootdx distribution retains its own upstream license in the
isolated environment and is pinned by requirements.lock.
