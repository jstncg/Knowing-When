Sample bodies in each source's published layout, used by tests/test_sources.py.

- bln_warn_integrated_sample.csv: six real rows recorded 2026-09-22 from Big Local News's consolidated WARN CSV (a Meta notice and its superseded version, a row from the 2022-04-07 bulk backfill, NY, TX, and a row without a notice date).
- nsf_awards_sample.json: NSF Awards API response.

The NSF sample is format-faithful, not a recording: api.nsf.gov was
unreachable from the environment that wrote it.

Paper-record samples used by tests/test_paper_sources.py, in the same spirit
(format-faithful, not recorded; arXiv, OpenReview and OpenAlex were unreachable
from the writing environment):

- arxiv_listing.xml: export.arxiv.org Atom feed for an author query (arxiv:affiliation set on one author).
- arxiv_abs_2501.01234.html: an abstract page's "Submission history" block with two versions.
- openreview_submissions.json / openreview_forum_sub{1,2}.json: API v2 notes for an author and two forums (one accepted, one rejected; sub2 uses v1-style plain content values).
- openalex_works.json / openalex_author.json / openalex_search.json: works listing, author profile with affiliation years, and an author search with two same-name hits.
