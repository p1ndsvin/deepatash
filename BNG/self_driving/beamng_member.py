import hashlib
import random
from typing import Tuple, Dict
import numpy as np
import logging as log
from datetime import datetime

from self_driving.beamng_config import BeamNGConfig
from self_driving.beamng_evaluator import BeamNGEvaluator
from core.member import Member
from self_driving.catmull_rom import catmull_rom
from self_driving.road_bbox import RoadBoundingBox
from self_driving.road_polygon import RoadPolygon
from self_driving.edit_distance_polyline import iterative_levenshtein
from core.config import Config
from core.timer import Timer

Tuple4F = Tuple[float, float, float, float]
Tuple2F = Tuple[float, float]


class BeamNGMember(Member):
    """A class representing a road returned by the RoadGenerator."""
    counter = 0

    def __init__(self, control_nodes: Tuple4F, sample_nodes: Tuple4F, num_spline_nodes: int,
                 road_bbox: RoadBoundingBox):
        super().__init__()
        BeamNGMember.counter += 1
        self.name = f'mbr{str(BeamNGMember.counter)}'
        self.name_ljust = self.name.ljust(7)
        self.control_nodes = control_nodes
        self.sample_nodes = sample_nodes
        self.num_spline_nodes = num_spline_nodes
        self.road_bbox = road_bbox
        self.config: BeamNGConfig = None
        self.problem: 'BeamNGProblem' = None
        self._evaluator: BeamNGEvaluator = None
        self.simulation = None
        self.rank = np.inf
        self.features = tuple()
        self.selected_counter = 0
        self.placed_mutant = 0
        self.timestamp = datetime.now()
        self.elapsed = Timer.get_elapsed_time()
        self.distance_to_boundary = None


    def clone(self):
        res = BeamNGMember(list(self.control_nodes), list(self.sample_nodes), self.num_spline_nodes, self.road_bbox)
        res.config = self.config
        res.problem = self.problem
        return res

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'control_nodes': self.control_nodes,
            'sample_nodes': self.sample_nodes,
            'num_spline_nodes': self.num_spline_nodes,
            'road_bbox_size': self.road_bbox.bbox.bounds,
            'distance_to_boundary': self.distance_to_boundary
        }

    @classmethod
    def from_dict(cls, dict: Dict):
        road_bbox = RoadBoundingBox(dict['road_bbox_size'])
        res = BeamNGMember([tuple(t) for t in dict['control_nodes']],
                           [tuple(t) for t in dict['sample_nodes']],
                           dict['num_spline_nodes'], road_bbox)
        res.distance_to_boundary = dict['distance_to_boundary']
        return res

    def evaluate(self):
        if self.needs_evaluation():
            self.simulation = self.problem._get_evaluator().evaluate([self])
            log.debug('eval', self)

        #assert not self.needs_evaluation()

    def needs_evaluation(self):
        return self.distance_to_boundary is None or self.simulation is None

    def clear_evaluation(self):
        self.distance_to_boundary = None

    def is_valid(self):
        return (RoadPolygon.from_nodes(self.sample_nodes).is_valid() and
                self.road_bbox.contains(RoadPolygon.from_nodes(self.control_nodes[1:-1])))

    def distance(self, other: 'BeamNGMember'):
        return iterative_levenshtein(self.sample_nodes, other.sample_nodes)

    def to_tuple(self):
        import numpy as np
        barycenter = np.mean(self.control_nodes, axis=0)[:2]
        return barycenter

    def mutate(self) -> 'BeamNGMember':
        flag = RoadMutator(self, lower_bound=-int(self.problem.config.MUTATION_EXTENT), upper_bound=int(self.problem.config.MUTATION_EXTENT)).mutate()
        self.distance_to_boundary = None
        if flag:
            return self
        else:
            return None

    def __repr__(self):
        eval_boundary = 'na'
        if self.distance_to_boundary:
            eval_boundary = str(self.distance_to_boundary)
            if self.distance_to_boundary > 0:
                eval_boundary = '+' + eval_boundary
            eval_boundary = '~' + eval_boundary
        eval_boundary = eval_boundary[:7].ljust(7)
        h = hashlib.sha256(str([tuple(node) for node in self.control_nodes]).encode('UTF-8')).hexdigest()[-5:]
        return f'{self.name_ljust} h={h} b={eval_boundary}'


class RoadMutator:
    NUM_UNDO_ATTEMPTS = 20

    def __init__(self, road: BeamNGMember, lower_bound=-2, upper_bound=2):
        self.road = road
        self.lower_bound = lower_bound
        self.higher_bound = upper_bound

    def mutate_gene(self, index, xy_prob=0.5) -> Tuple[int, int]:
        gene = list(self.road.control_nodes[index])
        # Choose the mutation extent
        mut_value = random.randint(self.lower_bound, self.higher_bound)
        # Avoid to choose 0
        if mut_value == 0:
            mut_value += 1
        c = 0
        if random.random() < xy_prob:
            c = 1
        gene[c] += mut_value
        self.road.control_nodes[index] = tuple(gene)
        self.road.sample_nodes = catmull_rom(self.road.control_nodes, self.road.num_spline_nodes)
        return c, mut_value

    def undo_mutation(self, index, c, mut_value):
        gene = list(self.road.control_nodes[index])
        gene[c] -= mut_value
        self.road.control_nodes[index] = tuple(gene)
        self.road.sample_nodes = catmull_rom(self.road.control_nodes, self.road.num_spline_nodes)

    def mutate(self, num_undo_attempts=10):
        backup_nodes = list(self.road.control_nodes)
        attempted_genes = set()
        n = len(self.road.control_nodes) - 2
        def next_gene_index() -> int:
            if len(attempted_genes) == n - 5:
                return -1
            i = random.randint(3, n-3)
            j = 0
            while i in attempted_genes:
                j += 1
                i = random.randint(3, n-3)
                if j > 1000000:
                    log.debug(attempted_genes)
                    return -1
            attempted_genes.add(i)
            assert 3 <= i <= n-3
            return i

        gene_index = next_gene_index()

        while gene_index != -1:
            c, mut_value = self.mutate_gene(gene_index)

            attempt = 0

            is_valid = self.road.is_valid()
            while not is_valid and attempt < num_undo_attempts:
                Config.INVALID += 1
                self.undo_mutation(gene_index, c, mut_value)
                c, mut_value = self.mutate_gene(gene_index)
                attempt += 1
                is_valid = self.road.is_valid()

            if is_valid:
                break
            else:
                gene_index = next_gene_index()

        if gene_index == -1:
            #raise ValueError("No gene can be mutated")
            log.info("No gene can be mutated")
            self.road.control_nodes = backup_nodes


        if self.road.is_valid() and self.road.control_nodes != backup_nodes:
            return True
        else:
            self.road.control_nodes = backup_nodes
            return False
