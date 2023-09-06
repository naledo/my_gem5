import math
import m5

from .abstract_ruby_cache_hierarchy import AbstractRubyCacheHierarchy
from ..abstract_two_level_cache_hierarchy import AbstractTwoLevelCacheHierarchy
from ....coherence_protocol import CoherenceProtocol
from ....isas import ISA
from ...boards.abstract_board import AbstractBoard
from ....utils.requires import requires

from .topologies.simple_pt2pt import SimplePt2Pt


from m5.objects import RubySystem, RubySequencer, DMASequencer, RubyPortProxy

from m5.objects import *

class L0Cache(RubyCache):
    pass


class L1Cache(RubyCache):
    pass


class L2Cache(RubyCache):
    pass


class MESIThreeLevelCacheHierarchy(
    AbstractRubyCacheHierarchy
):
    def __init__(
        self,
        l0i_size: str,
        l0i_assoc: int,
        l0d_size: str,
        l0d_assoc: int,
        l1_size: str,
        l1_assoc: int,
        l2_size: str,
        l2_assoc: int,
        num_clusters: int,
        num_cpus: int,
    ):
        AbstractRubyCacheHierarchy.__init__(self=self)

        self._l0i_size = l0i_size
        self._l0i_assoc = l0i_assoc
        self._l0d_size = l0d_size
        self._l0d_assoc = l0d_assoc
        self._l1_size = l1_size
        self._l1_assoc = l1_assoc
        self._l2_size = l2_size
        self._l2_assoc = l2_assoc
        self._num_clusters = num_clusters
        self._num_cpus = num_cpus
    
    def incorporate_cache(self, board: AbstractBoard) -> None:
        requires(coherence_protocol_required=CoherenceProtocol.MESI_THREE_LEVEL)
        
        cache_line_size = board.get_cache_line_size()

        self.ruby_system = RubySystem()

        self.ruby_system.number_of_virtual_networks = 5

        self.ruby_system.network = SimplePt2Pt(self.ruby_system)
        self.ruby_system.network.number_of_virtual_networks = 5

        self._l0_controllers = []
        self._l1_controllers = []
        self._l2_controllers = []

        assert self._num_cpus % self._num_clusters == 0
        num_cpus_per_cluster = self._num_cpus // self._num_clusters

        #assert self.num_l2caches % self.num_clusters == 0
        num_l2caches_per_cluster = 1
        
        l2_bits = int(math.log(num_l2caches_per_cluster, 2))
        block_size_bits = int(math.log(64, 2))
        l2_index_start = block_size_bits

        cores = board.get_processor().get_cores()

        for i in range(self._num_clusters):
            for j in range(num_cpus_per_cluster):

                core = cores[i*num_cpus_per_cluster+j]

                l0i_cache = L0Cache(
                    size=self._l0i_size,
                    assoc=self._l0i_assoc,
                    is_icache=True,
                    start_index_bit=block_size_bits,
                    replacement_policy=LRURP(), 
                )
                l0d_cache = L0Cache(
                    size=self._l0d_size,
                    assoc=self._l0d_assoc,
                    is_icache=False,
                    start_index_bit=block_size_bits,
                    replacement_policy=LRURP(),
                )

                clk_domain = board.get_clock_domain()

                prefetcher = RubyPrefetcher(
                    num_streams=16,
                    unit_filter=256,
                    nonunit_filter=256,
                    train_misses=5,
                    num_startup_pfs=4,
                    cross_page=True,
                )

                l0_cntrl = L0Cache_Controller(
                    version=i * num_cpus_per_cluster + j,
                    Icache=l0i_cache,
                    Dcache=l0d_cache,
                    transitions_per_cycle=32,
                    prefetcher=prefetcher,
                    enable_prefetch=False,
                    send_evictions=core.requires_send_evicts(),
                    clk_domain=clk_domain,
                    ruby_system=self.ruby_system,
                )

                cpu_seq = RubySequencer(
                    version=i * num_cpus_per_cluster + j,
                    clk_domain=clk_domain,
                    dcache=l0d_cache,
                    ruby_system=self.ruby_system,
                )

                l0_cntrl.sequencer = cpu_seq

                if board.has_io_bus():
                    l0_cntrl.sequencer.connectIOPorts(board.get_io_bus())

                core.connect_icache(l0_cntrl.sequencer.in_ports)
                core.connect_dcache(l0_cntrl.sequencer.in_ports)

                core.connect_walker_ports(
                    l0_cntrl.sequencer.in_ports, l0_cntrl.sequencer.in_ports
                )

                if board.get_processor().get_isa() == ISA.X86:
                    int_req_port = l0_cntrl.sequencer.interrupt_out_port
                    int_resp_port = l0_cntrl.sequencer.in_ports
                    core.connect_interrupt(int_req_port, int_resp_port)
                else:
                    core.connect_interrupt()
                
                
                l1_cache = L1Cache(
                    size=self._l1_size,
                    assoc=self._l1_assoc,
                    start_index_bit=block_size_bits,
                    is_icache=False,
                )

                l1_cntrl = L1Cache_Controller(
                    version=i * num_cpus_per_cluster + j,
                    cache=l1_cache,
                    l2_select_num_bits=l2_bits,
                    cluster_id=i,
                    transitions_per_cycle=32,
                    ruby_system=self.ruby_system,
                )
                
                self._l0_controllers.append(l0_cntrl)
                self._l1_controllers.append(l1_cntrl)

                l0_cntrl.prefetchQueue = MessageBuffer()
                l0_cntrl.mandatoryQueue = MessageBuffer()
                l0_cntrl.bufferToL1 = MessageBuffer(ordered=True)
                l1_cntrl.bufferFromL0 = l0_cntrl.bufferToL1
                l0_cntrl.bufferFromL1 = MessageBuffer(ordered=True)
                l1_cntrl.bufferToL0 = l0_cntrl.bufferFromL1

                l1_cntrl.requestToL2 = MessageBuffer()
                l1_cntrl.requestToL2.out_port = self.ruby_system.network.in_port
                l1_cntrl.responseToL2 = MessageBuffer()
                l1_cntrl.responseToL2.out_port = self.ruby_system.network.in_port
                l1_cntrl.unblockToL2 = MessageBuffer()
                l1_cntrl.unblockToL2.out_port = self.ruby_system.network.in_port

                l1_cntrl.requestFromL2 = MessageBuffer()
                l1_cntrl.requestFromL2.in_port = self.ruby_system.network.out_port
                l1_cntrl.responseFromL2 = MessageBuffer()
                l1_cntrl.responseFromL2.in_port = self.ruby_system.network.out_port

            for j in range(num_l2caches_per_cluster):
                l2_cache = L2Cache(
                    size=self._l2_size,
                    assoc=self._l2_assoc,
                    start_index_bit=l2_index_start,
                )
                l2_cntrl = L2Cache_Controller(
                    version=i * num_l2caches_per_cluster + j,
                    L2cache=l2_cache,
                    cluster_id=i,
                    transitions_per_cycle=4,
                    ruby_system=self.ruby_system,
                )

                self._l2_controllers.append(l2_cntrl)

                l2_cntrl.DirRequestFromL2Cache = MessageBuffer()
                l2_cntrl.DirRequestFromL2Cache.out_port = (
                    self.ruby_system.network.in_port
                )
                l2_cntrl.L1RequestFromL2Cache = MessageBuffer()
                l2_cntrl.L1RequestFromL2Cache.out_port = (
                    self.ruby_system.network.in_port
                )
                l2_cntrl.responseFromL2Cache = MessageBuffer()
                l2_cntrl.responseFromL2Cache.out_port = self.ruby_system.network.in_port

                l2_cntrl.unblockToL2Cache = MessageBuffer()
                l2_cntrl.unblockToL2Cache.in_port = self.ruby_system.network.out_port
                l2_cntrl.L1RequestToL2Cache = MessageBuffer()
                l2_cntrl.L1RequestToL2Cache.in_port = self.ruby_system.network.out_port
                l2_cntrl.responseToL2Cache = MessageBuffer()
                l2_cntrl.responseToL2Cache.in_port = self.ruby_system.network.out_port


        self._directory_controllers = []
        num_dirs = self._num_clusters

        for i in range(num_dirs):
            dir_cntrl = Directory_Controller()
            dir_cntrl.version = i
            dir_cntrl.directory = RubyDirectoryMemory()
            dir_cntrl.ruby_system = self.ruby_system

            self._directory_controllers.append(dir_cntrl)
        
        for dir_cntrl in self._directory_controllers:
            dir_cntrl.requestToDir = MessageBuffer()
            dir_cntrl.requestToDir.in_port = self.ruby_system.network.out_port
            dir_cntrl.responseToDir = MessageBuffer()
            dir_cntrl.responseToDir.in_port = self.ruby_system.network.out_port
            dir_cntrl.responseFromDir = MessageBuffer()
            dir_cntrl.responseFromDir.out_port = self.ruby_system.network.in_port
            dir_cntrl.requestToMemory = MessageBuffer()
            dir_cntrl.responseFromMemory = MessageBuffer()
        
        self._dma_controllers = []
        if board.has_dma_ports():
            
            dma_ports = board.get_dma_ports()

            for i, port in enumerate(dma_ports):
                dma_seq = DMASequencer(version=i, ruby_system=self.ruby_system)
                dma_cntrl = DMA_Controller(
                    version=i,
                    dma_sequencer=dma_seq,
                    transitions_per_cycle=4,
                    ruby_system=self.ruby_system,
                )

                self._dma_controllers.append(dma_cntrl)

                dma_cntrl.mandatoryQueue = MessageBuffer()
                dma_cntrl.responseFromDir = MessageBuffer(ordered=True)
                dma_cntrl.responseFromDir.in_port = self.ruby_system.network.out_port
                dma_cntrl.requestToDir = MessageBuffer()
                dma_cntrl.requestToDir.out_port = self.ruby_system.network.in_port

        self.ruby_system.num_of_sequencers = len(self._l0_controllers) + len(
            self._dma_controllers
        )

        self.ruby_system.l0_controllers = self._l0_controllers
        self.ruby_system.l1_controllers = self._l1_controllers
        self.ruby_system.l2_controllers = self._l2_controllers
        self.ruby_system.directory_controllers = self._directory_controllers
        
        if len(self._dma_controllers) != 0:
            self.ruby_system.dma_controllers = self._dma_controllers
        
        # Create the network and connect the controllers.
        self.ruby_system.network.connectControllers(
            self._l0_controllers
            + self._l1_controllers
            + self._l2_controllers
            + self._directory_controllers
            + self._dma_controllers
        )
        self.ruby_system.network.setup_buffers()
        # Set up a proxy port for the system_port. Used for load binaries and
        # other functional-only things.
        self.ruby_system.sys_port_proxy = RubyPortProxy()
        board.connect_system_port(self.ruby_system.sys_port_proxy.in_ports)

