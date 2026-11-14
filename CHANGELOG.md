# Changelog

## 0.2.1

- Same code as 0.2.0, which never reached PyPI: a release test read INSTALL.txt, which is README.md in the public repo.

## 0.2.0

Breaking:
- `uncertain_words`: `page_size` is now a list `[{"page": 1, "width": 2480, "height": 3508}]` (it was an object with the page number as key, which JSON turns into a string). `page` is a number everywhere.
- `get_result`: `format` has no default any more (json is used when you leave it out). With `pages` or `search` it, `out_dir` and `source_path` are refused, as is giving `pages` and `search` together; before they were ignored.
- `uncertain_words`: `limit` outside 1 to 100 and `max_confidence` outside 0 to 1 are errors (before they were clamped or accepted).

Changed:
- An empty `pages` or `search`, and a page the job does not have, are errors that say what is wrong (before: a file was written, or an empty text came back).
- `OCR_ALLOWED_DIR` is checked before the file is looked at: outside it the answer is the same whether the path exists or not. A relative path gets the hint to give an absolute one.
- `get_result` of a job this server did not start says in `note` where the file went (`~/Documents/ocr-results`, or `~/ocr-results` without a `Documents` folder).
- The hint for several columns also comes on short pages, only where it is likely right (a spread or text in columns, not tables, forms or head boxes), and says that no pipeline reads columns in order yet.

## 0.1.0

First release.
