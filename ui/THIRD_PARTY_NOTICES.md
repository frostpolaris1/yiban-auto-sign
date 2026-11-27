# Third-Party Notices

## Adminator admin dashboard

- Source: https://github.com/puikinsh/adminator-admin-dashboard
- Version: 4.4.0
- Commit used: `4c768545ad0c981c77669a7bf84de75b94d082e0`
- Files used: `src/assets/styles/**` and the page markup in `src/*.html`
- License: MIT

The design tokens in `src/styles/tokens.css` and the markup structure of the
shell components are derived from Adminator. `src/styles/adminator.css` is a
byte-for-byte copy of the vendor stylesheet.

```
MIT License

Copyright (c) 2018 Aigars Silkalns

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
```

## Fonts

### Noto Sans SC (subset)

- Source: https://fonts.google.com/noto/specimen/Noto+Sans+SC
- Upstream: https://github.com/notofonts/noto-cjk
- File: `public/fonts/NotoSansSC-VF-subset.woff2`
- Modification: subset to the UI character set with fontTools/pyftsubset; the
  variable `wght` axis is retained.
- License: SIL Open Font License 1.1. Full text:
  `public/fonts/OFL-NotoSansSC.txt`.

### Fraunces (subset)

- Source: https://fonts.google.com/specimen/Fraunces
- Upstream: https://github.com/undercasetype/Fraunces
- File: `public/fonts/Fraunces-subset.woff2`
- Modification: subset to numerals, punctuation and Latin UI characters with
  fontTools/pyftsubset; the variable axes are retained.
- License: SIL Open Font License 1.1. Full text:
  `public/fonts/OFL-Fraunces.txt`.

HarmonyOS Sans SC is listed first in the UI font stack for devices that already
have it installed. The repository does not redistribute HarmonyOS Sans files;
its license does not permit modified (including subsetted) copies.
