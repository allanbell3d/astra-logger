#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0,str(Path.home()/".hermes/scripts"))
from astra.runtime import pre_script
pre_script('rca')
