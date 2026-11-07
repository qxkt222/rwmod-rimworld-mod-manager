"""Download pipeline internals — split out of ``rwmod.downloader``.

The public import path stays ``rwmod.downloader`` (that module re-exports
everything); this package only organises the implementation by responsibility:

* :mod:`rwmod.download.outcomes` — structured outcomes and batch results;
* :mod:`rwmod.download.locks` — per-mod / per-destination lock registry;
* :mod:`rwmod.download.install` — verify, stash, backup and rollback helpers;
* :mod:`rwmod.download.deps` — dependency collection;
* :mod:`rwmod.download.core` — the single-mod download state machine.
"""
