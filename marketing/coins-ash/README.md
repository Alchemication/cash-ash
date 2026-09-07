# Coin-and-ash imagery

The one photograph on the public site. Generated with ChatGPT's image model
from the brief in `prompt.md`; `source.png` is the untouched output.

Derived files live in `marketing/site/assets/` and are what the page ships:

| File | Size | Use |
| --- | --- | --- |
| `coins-ash.jpg` | 1600x840 | Full-width banner under the hero |
| `og-image.jpg` | 1200x630 | Link preview (`og:image`) |

Both are plain resamples of the source, no crop, made with macOS `sips`:

```bash
cp marketing/coins-ash/source.png marketing/site/assets/coins-ash.jpg
sips -s format jpeg -s formatOptions 82 --resampleWidth 1600 marketing/site/assets/coins-ash.jpg
cp marketing/coins-ash/source.png marketing/site/assets/og-image.jpg
sips -s format jpeg -s formatOptions 82 --resampleWidth 1200 marketing/site/assets/og-image.jpg
```

Regenerate both if the source changes. The page's `object-position` assumes the
coins sit left of centre; a new composition may need that adjusted in
`marketing/site/index.html`.
