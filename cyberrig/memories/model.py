"""Memory channel model: a snapshot of freq/mode/power/filter width/ATT/preamp."""

from dataclasses import dataclass


@dataclass
class MemoryChannel:
    name:   str = "Memory"
    freq:   int = 14200000   # Hz
    mode:   str = "USB"
    power:  int = 100        # watts
    sh:     int = 13         # filter width code (Table 3, meaning depends on mode)
    att:    str = "OFF"
    preamp: str = "IPO"

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryChannel":
        m = cls()
        for k, v in d.items():
            if hasattr(m, k):
                setattr(m, k, v)
        return m
