# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class Logger(logging.Logger):
    def __init__(
        self,
        log_path: str | Path | None = None,
        *,
        debug: bool = False,
    ) -> None:
        level = logging.DEBUG if debug else logging.INFO
        super().__init__(name="Logger", level=level)
        # `logging.Handler`, which is the type the two branches share:
        # inferred from the first of them instead, the second is a
        # StreamHandler assigned to a name holding a FileHandler. What
        # stood here before the branch was a third handler, built on
        # every path and used on none.
        handler: logging.Handler
        handler = logging.FileHandler(log_path) if log_path else logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(message)s")
        handler.setFormatter(formatter)
        self.addHandler(handler)

    def close(self) -> None:
        for handler in self.handlers:
            handler.close()
            self.removeHandler(handler)
