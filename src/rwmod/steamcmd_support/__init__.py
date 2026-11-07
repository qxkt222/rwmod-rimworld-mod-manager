"""Internal layers of the SteamCMD wrapper (``rwmod.steamcmd``).

Not part of the public API: import ``SteamCMD``, ``DownloadResult``,
``ErrorKind``, ``cancel_download`` and ``request_cancel`` from
``rwmod.steamcmd``. The package exists to keep log parsing, the result
taxonomy and the live-process registry separate from the subprocess driver.
"""
