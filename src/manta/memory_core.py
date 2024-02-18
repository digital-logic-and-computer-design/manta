from amaranth import *
from manta.utils import *
from math import ceil


class ReadOnlyMemoryCore(Elaboratable):
    """
    A module for generating a memory on the FPGA, with a read port tied to
    Manta's internal bus, and a write port provided to user logic.

    Provides methods for generating synthesizable logic for the FPGA, as well
    as methods for reading and writing the value of a register.

    More information available in the online documentation at:
    https://fischermoseley.github.io/manta/memory_core/
    """

    def __init__(self, config, base_addr, interface):
        self._config = config
        self._base_addr = base_addr
        self._interface = interface
        self._check_config(config)

        self._depth = self._config["depth"]
        self._width = self._config["width"]
        self._max_addr = self._base_addr + (self._depth * ceil(self._width / 16))

        # Bus Connections
        self.bus_i = Signal(InternalBus())
        self.bus_o = Signal(InternalBus())

        # User Port
        self.user_addr = Signal(range(self._depth))
        self.user_data = Signal(self._width)
        self.user_we = Signal(1)

        self._define_mems()

    def _check_config(self, config):
        # Check for unrecognized options
        valid_options = ["type", "depth", "width"]
        for option in config:
            if option not in valid_options:
                warn(f"Ignoring unrecognized option '{option}' in memory core.")

        # Check depth is provided and positive
        if "depth" not in config:
            raise ValueError("Depth of memory core must be specified.")

        if not isinstance(config["depth"], int):
            raise ValueError("Depth of memory core must be an integer.")

        if config["depth"] <= 0:
            raise ValueError("Depth of memory core must be positive. ")

        # Check width is provided and positive
        if "width" not in config:
            raise ValueError("Width of memory core must be specified.")

        if not isinstance(config["width"], int):
            raise ValueError("Width of memory core must be an integer.")

        if config["width"] <= 0:
            raise ValueError("Width of memory core must be positive. ")

    def _pipeline_bus(self, m):
        self._bus_pipe = [Signal(InternalBus()) for _ in range(3)]
        m.d.sync += self._bus_pipe[0].eq(self.bus_i)

        for i in range(1, 3):
            m.d.sync += self._bus_pipe[i].eq(self._bus_pipe[i - 1])

        m.d.sync += self.bus_o.eq(self._bus_pipe[2])

    def _define_mems(self):
        # There's three cases that must be handled:
        # 1. Integer number of 16 bit mems
        # 2. Integer number of 16 bit mems + partial mem
        # 3. Just the partial mem (width < 16)

        # Only one, partial-width memory is needed
        if self._width < 16:
            self._mems = [Memory(depth=self._depth, width=self._width)]

        # Only full-width memories are needed
        elif self._width % 16 == 0:
            self._mems = [
                Memory(depth=self._depth, width=16) for _ in range(self._width // 16)
            ]

        # Both full-width and partial memories are needed
        else:
            self._mems = [
                Memory(depth=self._depth, width=16) for i in range(self._width // 16)
            ]
            self._mems += [Memory(depth=self._depth, width=self._width % 16)]

    def _handle_read_ports(self, m):
        # These are tied to the bus
        for i, mem in enumerate(self._mems):
            read_port = mem.read_port()
            m.d.comb += read_port.en.eq(1)

            start_addr = self._base_addr + (i * self._depth)
            stop_addr = start_addr + self._depth - 1

            # Throw BRAM operations into the front of the pipeline
            with m.If(
                (self.bus_i.valid)
                & (~self.bus_i.rw)
                & (self.bus_i.addr >= start_addr)
                & (self.bus_i.addr <= stop_addr)
            ):
                m.d.sync += read_port.addr.eq(self.bus_i.addr - start_addr)

            # Pull BRAM reads from the back of the pipeline
            with m.If(
                (self._bus_pipe[2].valid)
                & (~self._bus_pipe[2].rw)
                & (self._bus_pipe[2].addr >= start_addr)
                & (self._bus_pipe[2].addr <= stop_addr)
            ):
                m.d.sync += self.bus_o.data.eq(read_port.data)

    def _handle_write_ports(self, m):
        # These are given to the user
        for i, mem in enumerate(self._mems):
            write_port = mem.write_port()

            m.d.comb += write_port.addr.eq(self.user_addr)
            m.d.comb += write_port.data.eq(self.user_data[16 * i : 16 * (i + 1)])
            m.d.comb += write_port.en.eq(self.user_we)

    def elaborate(self, platform):
        m = Module()

        # Add memories as submodules
        for i, mem in enumerate(self._mems):
            m.submodules[f"mem_{i}"] = mem

        self._pipeline_bus(m)
        self._handle_read_ports(m)
        self._handle_write_ports(m)
        return m

    def get_top_level_ports(self):
        """
        Return the Amaranth signals that should be included as ports in the
        top-level Manta module.
        """
        return [self.user_addr, self.user_data, self.user_we]

    def get_max_addr(self):
        """
        Return the maximum addresses in memory used by the core. The address space used
        by the core extends from `base_addr` to the number returned by this function.
        """
        return self._max_addr

    def read_from_user_addr(self, addrs):
        """
        Read the memory stored at the provided address, as seen from the user side.
        """

        # Convert user address space to bus address space
        #   (for instance, for a core with base address 10 and width 33,
        #   reading from address 4 is actually a read from address 14
        #   and address 14 + depth, and address 14 + 2*depth)

        if isinstance(addrs, int):
            return self.read_from_user_addr([addrs])[0]

        bus_addrs = []
        for addr in addrs:
            bus_addrs += [
                addr + self._base_addr + i * self._depth for i in range(len(self._mems))
            ]

        datas = self._interface.read(bus_addrs)
        data_chunks = split_into_chunks(datas, len(self._mems))
        return [words_to_value(chunk) for chunk in data_chunks]

    # def write_to_user_addr(self, addrs, datas):
    #     """
    #     Read from the address
    #     """

    #     bus_addrs = []
    #     for addr in addrs:
    #         bus_addrs += [
    #             addr + self._base_addr + i * self._depth for i in range(len(self._mems))
    #         ]

    #     bus_datas = []
    #     for data in datas:
    #         bus_datas += value_to_words(data)

    #     self._interface.write(bus_addrs, bus_datas)
