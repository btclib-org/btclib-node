# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.

import logging


class Logger(logging.Logger):
    def __init__(self, log_path=None, debug=False, **kwargs):
        level = logging.DEBUG if debug else logging.INFO
        super().__init__(name="Logger", level=level, **kwargs)
        handler = logging.StreamHandler()
        if log_path:
            handler = logging.FileHandler(log_path)
        else:
            handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(message)s")
        handler.setFormatter(formatter)
        self.addHandler(handler)

    def close(self):
        for handler in self.handlers:
            handler.close()
            self.removeHandler(handler)
