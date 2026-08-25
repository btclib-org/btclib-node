# Copyright (c) The btclib developers
# Distributed under the MIT software license, see the accompanying
# LICENSE file or https://opensource.org/license/mit for the full text.


from typing import TYPE_CHECKING

from btclib_node.chains import RegTest
from btclib_node.chainstate import Chainstate
from btclib_node.log import Logger

if TYPE_CHECKING:
    from pathlib import Path


def test_init(tmp_path: Path) -> None:
    Chainstate(tmp_path, RegTest(), Logger(debug=True)).close()
