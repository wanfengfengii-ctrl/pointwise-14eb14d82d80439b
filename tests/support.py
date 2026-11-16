import unittest
from lake import Lake


class MemoryCase(unittest.TestCase):
    def setUp(self):
        self.lake = Lake()
        self.addCleanup(self.lake.close)


SIMPLE = {"type": "record", "name": "Reading", "fields": [{"name": "id", "type": "int"}]}
