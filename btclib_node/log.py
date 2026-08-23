# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import logging
from pathlib import Path
from typing import Any


class Logger(logging.Logger):
    def __init__(
        self,
        log_path: str | Path | None = None,
        debug: bool = False,
        **kwargs: Any,
    ) -> None:
        level = logging.DEBUG if debug else logging.INFO
        super().__init__(name="Logger", level=level, **kwargs)
        # `logging.Handler`, which is the type the two branches share:
        # inferred from the first of them instead, the second is a
        # StreamHandler assigned to a name holding a FileHandler. What
        # stood here before the branch was a third handler, built on
        # every path and used on none.
        handler: logging.Handler
        if log_path:
            handler = logging.FileHandler(log_path)
        else:
            handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(message)s")
        handler.setFormatter(formatter)
        self.addHandler(handler)

    def close(self) -> None:
        for handler in self.handlers:
            handler.close()
            self.removeHandler(handler)
