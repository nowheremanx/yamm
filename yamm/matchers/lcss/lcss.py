import functools as ft
import logging
from multiprocessing import Pool

from shapely.geometry import Point

from yamm.constructs.coordinate import Coordinate
from yamm.maps.map_interface import MapInterface
from yamm.matchers.lcss.constructs import TrajectorySegment
from yamm.matchers.lcss.ops import (
    new_path,
    split_trajectory_segment,
    same_trajectory_scheme,
    find_stationary_points,
    drop_stationary_points,
    add_matches_for_stationary_points,
)
from yamm.matchers.matcher_interface import *
from yamm.utils.crs import XY_CRS

log = logging.getLogger(__name__)


class LCSSMatcher(MatcherInterface):
    """
    A map matcher based on the paper:

    Zhu, Lei, Jacob R. Holden, and Jeffrey D. Gonder.
    "Trajectory Segmentation Map-Matching Approach for Large-Scale, High-Resolution GPS Data."
    Transportation Research Record: Journal of the Transportation Research Board 2645 (2017): 67-75.

    """

    def __init__(
        self,
        road_map: MapInterface,
        distance_epsilon: float = 50.0,
        similarity_cutoff: float = 0.9,
        cutting_threshold: float = 10.0,
        random_cuts: int = 0,
        distance_threshold: float = 10000,
    ):
        self.road_map = road_map
        self.distance_epsilon = distance_epsilon
        self.similarity_cutoff = similarity_cutoff
        self.cutting_threshold = cutting_threshold
        self.random_cuts = random_cuts
        self.distance_threshold = distance_threshold

    def match_trace(self, trace: Trace) -> MatchResult:
        def _join_segment(a: TrajectorySegment, b: TrajectorySegment):
            new_traces = a.trace + b.trace
            new_path = a.path + b.path

            # test to see if there is a gap between the paths and if so,
            # try to connect it
            if len(a.path) > 1 and len(b.path) > 1:
                end_road = a.path[-1]
                start_road = b.path[0]
                if end_road.metadata["v"] != start_road.metadata["u"]:
                    o = Coordinate(
                        coordinate_id=None,
                        geom=Point(end_road.geom.coords[-1]),
                        crs=XY_CRS,
                    )
                    d = Coordinate(
                        coordinate_id=None,
                        geom=Point(start_road.geom.coords[0]),
                        crs=XY_CRS,
                    )
                    path = self.road_map.shortest_path(o, d)
                    new_path = a.path + path + b.path

            return TrajectorySegment(new_traces, new_path)

        stationary_index = find_stationary_points(trace)

        sub_trace = drop_stationary_points(trace, stationary_index)

        road_map = self.road_map
        de = self.distance_epsilon
        ct = self.cutting_threshold
        rc = self.random_cuts
        dt = self.distance_threshold
        initial_segment = (
            TrajectorySegment(trace=sub_trace, path=new_path(road_map, sub_trace, de))
            .score_and_match(de, dt)
            .compute_cutting_points(de, ct, rc)
        )

        initial_scheme = split_trajectory_segment(road_map, initial_segment, de)
        scheme = initial_scheme

        n = 0
        while n < 10:
            next_scheme = []
            for segment in scheme:
                scored_segment = segment.score_and_match(de, dt).compute_cutting_points(
                    de, ct, rc
                )
                if scored_segment.score >= self.similarity_cutoff:
                    next_scheme.append(scored_segment)
                else:
                    # split and check the score
                    new_split = split_trajectory_segment(road_map, scored_segment, de)
                    joined_segment = ft.reduce(
                        _join_segment, new_split
                    ).score_and_match(de, dt)
                    if joined_segment.score > scored_segment.score:
                        # we found a better fit
                        next_scheme.extend(new_split)
                    else:
                        next_scheme.append(scored_segment)
            n += 1
            if same_trajectory_scheme(scheme, next_scheme):
                break

            scheme = next_scheme

        joined_segment = ft.reduce(_join_segment, scheme).score_and_match(de, dt)

        matches = joined_segment.matches

        matches_w_stationary_points = add_matches_for_stationary_points(
            matches, stationary_index
        )

        return matches_w_stationary_points

    def match_trace_batch(
        self,
        trace_batch: List[Trace],
        processes: int = 1,
    ) -> List[MatchResult]:
        """
        match traces in batches; useful for large traces as the computational complexity of the scoring
        function is O(N^2)

        :param trace_batch:
        :param processes:
        :return:
        """

        if processes > 1:
            results = [self.match_trace(t) for t in trace_batch]
        else:
            with Pool(processes=processes) as p:
                results = p.map(self.match_trace, trace_batch)

        return results
