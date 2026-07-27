# Geist font source and license

`geist-latin.woff2` in this directory is the real Geist Latin variable font,
vendored directly into the widget so the widget's build has no dependency on
any other app's `node_modules`. Vite inlines it as a `data:font/woff2` URI
inside the widget's single injected stylesheet at build time; the visitor's
browser never requests it over the network. Geist is distributed under the
SIL Open Font License 1.1.
