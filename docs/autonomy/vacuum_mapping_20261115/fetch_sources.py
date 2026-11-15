"""Archive public research over HTTP; never accesses robot/HA credentials.

Run from the repository root. Raw upstream documents stay in ignored local
storage; the small source ledger records identity, hashes and retrieval gaps.
"""
from concurrent.futures import ThreadPoolExecutor
import argparse
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
RAW = ROOT / '.local-deploy/vacuum-mapping-20261115/public-sources'
CATALOG = [
    ('S01', 'Probabilistic Robotics: occupancy mapping and localization', 'https://www.probabilistic-robotics.org/', 'textbook'),
    ('S02', 'Thrun: Robotic Mapping - A Survey', 'https://robots.stanford.edu/papers/thrun.mapping-tr.pdf', 'survey'),
    ('S03', 'Yamauchi: A Frontier-Based Approach for Autonomous Exploration', 'https://www.cs.cmu.edu/~motionplanning/papers/sbp_papers/integrated1/yamauchi_frontiers.pdf', 'frontier'),
    ('S04', 'Hess et al.: Real-Time Loop Closure in 2D LIDAR SLAM', 'https://research.google/pubs/real-time-loop-closure-in-2d-lidar-slam/', 'slam'),
    ('S05', 'SLAM Toolbox upstream documentation', 'https://raw.githubusercontent.com/SteveMacenski/slam_toolbox/ros2/README.md', 'slam'),
    ('S06', 'SLAM Toolbox: SLAM for the dynamic world', 'https://arxiv.org/abs/2103.05612', 'paper'),
    ('S07', 'ROS REP-105 coordinate frames', 'https://www.ros.org/reps/rep-0105.html', 'frames'),
    ('S08', 'ROS REP-103 units and coordinate conventions', 'https://www.ros.org/reps/rep-0103.html', 'frames'),
    ('S09', 'Nav2: mapping and localization', 'https://docs.nav2.org/setup_guides/sensors/mapping_localization.html', 'navigation'),
    ('S10', 'Nav2: inflation layer', 'https://docs.nav2.org/configuration/packages/costmap-plugins/inflation.html', 'costmap'),
    ('S11', 'Nav2: obstacle layer', 'https://docs.nav2.org/configuration/packages/costmap-plugins/obstacle.html', 'costmap'),
    ('S12', 'Nav2: voxel layer', 'https://docs.nav2.org/configuration/packages/costmap-plugins/voxel.html', 'costmap'),
    ('S13', 'Nav2: collision monitor', 'https://docs.nav2.org/configuration/packages/collision_monitor/configuring-collision-monitor-node.html', 'safety'),
    ('S14', 'robot_localization upstream', 'https://raw.githubusercontent.com/cra-ros-pkg/robot_localization/ros2/README.md', 'odometry'),
    ('S15', 'RTAB-Map as an open-source lidar and visual SLAM library', 'https://arxiv.org/abs/1809.06104', 'rgbd-slam'),
    ('S16', 'RTAB-Map ROS wrapper', 'https://raw.githubusercontent.com/introlab/rtabmap_ros/ros2/README.md', 'rgbd-slam'),
    ('S17', 'ORB-SLAM3', 'https://arxiv.org/abs/2007.11898', 'visual-slam'),
    ('S18', 'm-explore ROS2 frontier exploration', 'https://raw.githubusercontent.com/robo-friends/m-explore-ros2/main/README.md', 'exploration'),
    ('S19', 'Choset and Pignon: Boustrophedon cellular decomposition', 'https://www.ri.cmu.edu/publications/coverage-path-planning-the-boustrophedon-decomposition/', 'coverage'),
    ('S20', 'Fields2Cover coverage planning', 'https://raw.githubusercontent.com/Fields2Cover/Fields2Cover/main/README.md', 'coverage'),
    ('S21', 'Valetudo: supported robots', 'https://valetudo.cloud/pages/general/supported-robots.html', 'vacuum'),
    ('S22', 'Valetudo map data structures', 'https://raw.githubusercontent.com/Hypfer/Valetudo/master/backend/lib/entities/map/ValetudoMap.js', 'map-format'),
    ('S23', 'Dreame Vacuum Home Assistant integration', 'https://raw.githubusercontent.com/Tasshack/dreame-vacuum/master/README.md', 'dreame'),
    ('S24', 'Dreame map decoder upstream', 'https://raw.githubusercontent.com/Tasshack/dreame-vacuum/master/custom_components/dreame_vacuum/dreame/map.py', 'map-format'),
    ('S25', 'Xiaomi Cloud Map Extractor', 'https://raw.githubusercontent.com/PiotrMachowski/Home-Assistant-custom-components-Xiaomi-Cloud-Map-Extractor/master/README.md', 'map-format'),
    ('S26', 'python-miio: vacuum API', 'https://raw.githubusercontent.com/rytilahti/python-miio/master/miio/integrations/vacuum/roborock/vacuum.py', 'device-api'),
    ('S27', 'Xiaomi Home integration licensing and architecture', 'https://raw.githubusercontent.com/XiaoMi/ha_xiaomi_home/main/README.md', 'device-api'),
    ('S28', 'Home Assistant REST API', 'https://developers.home-assistant.io/docs/api/rest/', 'ha-api'),
    ('S29', 'ROS LaserScan message contract', 'https://docs.ros.org/en/rolling/p/sensor_msgs/msg/LaserScan.html', 'sensor-contract'),
    ('S30', 'Borenstein and Feng: UMBmark systematic odometry calibration', 'https://deepblue.lib.umich.edu/handle/2027.42/3758', 'calibration'),
    ('S31', 'SLAMTEC RPLIDAR ROS2 driver', 'https://raw.githubusercontent.com/Slamtec/sllidar_ros2/main/README.md', 'lidar'),
    ('S32', 'TUM RGB-D SLAM evaluation', 'https://cvg.cit.tum.de/data/datasets/rgbd-dataset/tools', 'evaluation'),
    ('S33', 'RTAB-Map upstream source and paper references', 'https://raw.githubusercontent.com/introlab/rtabmap/master/README.md', 'rgbd-slam'),
    ('S34', 'ORB-SLAM3 license', 'https://raw.githubusercontent.com/UZ-SLAMLab/ORB_SLAM3/master/LICENSE', 'license'),
    ('S35', 'Xiaomi Home integration license', 'https://raw.githubusercontent.com/XiaoMi/ha_xiaomi_home/main/LICENSE.md', 'license'),
    ('S36', 'SLAM Toolbox package metadata and license declaration', 'https://raw.githubusercontent.com/SteveMacenski/slam_toolbox/ros2/package.xml', 'license'),
    ('S37', 'Valetudo MapLayer pixel coordinates and run-length contract', 'https://raw.githubusercontent.com/Hypfer/Valetudo/master/backend/lib/entities/map/MapLayer.js', 'map-format'),
]

FALLBACKS = {
    'S01': 'https://mitpress.mit.edu/9780262201629/probabilistic-robotics/',
    'S09': 'https://raw.githubusercontent.com/ros-navigation/navigation2/main/README.md',
    'S10': 'https://raw.githubusercontent.com/ros-navigation/navigation2/main/nav2_costmap_2d/plugins/inflation_layer.cpp',
    'S11': 'https://raw.githubusercontent.com/ros-navigation/navigation2/main/nav2_costmap_2d/plugins/obstacle_layer.cpp',
    'S12': 'https://raw.githubusercontent.com/ros-navigation/navigation2/main/nav2_costmap_2d/plugins/voxel_layer.cpp',
    'S13': 'https://raw.githubusercontent.com/ros-navigation/navigation2/main/nav2_collision_monitor/README.md',
    'S21': 'https://raw.githubusercontent.com/Hypfer/Valetudo/master/docs/pages/general/supported-robots.md',
    'S26': 'https://raw.githubusercontent.com/rytilahti/python-miio/master/miio/integrations/vacuum/roborock/vacuum/vacuum.py',
    'S06': 'https://joss.theoj.org/papers/10.21105/joss.02783',
    'S15': 'https://introlab.github.io/rtabmap/',
    'S29': 'https://raw.githubusercontent.com/ros2/common_interfaces/rolling/sensor_msgs/msg/LaserScan.msg',
}
EXPECTED = {'S06': 'slam toolbox', 'S15': 'rtab-map', 'S29': 'angle_increment'}


class Visible(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def fetch(item):
    sid, title, url, topic = item
    record = dict(id=sid, title=title, url=url, topic=topic,
                  retrieved_at=datetime.now(timezone.utc).isoformat())
    try:
        req = Request(url, headers={'User-Agent': 'XLeRobot-public-research/1.0'})
        with urlopen(req, timeout=25) as response:
            raw = response.read(12_000_001)
            if len(raw) > 12_000_000:
                raise ValueError('source exceeds 12 MB research budget')
            kind, final = response.headers.get('Content-Type', ''), response.url
        suffix = '.pdf' if raw.startswith(b'%PDF') else '.txt'
        (RAW / (sid + suffix)).write_bytes(raw)
        text = ''
        if suffix != '.pdf':
            decoded = raw.decode('utf-8', errors='replace')
            if 'html' in kind:
                parser = Visible(); parser.feed(decoded)
                decoded = ' '.join(parser.parts)
            text = re.sub(r'\s+', ' ', decoded).strip()
            (RAW / (sid + '.extracted.txt')).write_text(text, encoding='utf-8')
        record.update(status='retrieved', final_url=final, bytes=len(raw),
                      sha256=hashlib.sha256(raw).hexdigest(), content_type=kind,
                      opening_excerpt=text[:650])
        if 'making sure you\'re not a bot' in text[:500].lower():
            record.update(status='challenge_page', error='HTTP success returned a bot challenge, not the requested document')
        elif sid in EXPECTED and EXPECTED[sid] not in text.lower():
            record.update(status='identity_mismatch', error='Retrieved title/content did not identify the intended source')
    except Exception as exc:
        record.update(status='unavailable', error=type(exc).__name__ + ': ' + str(exc)[:160])
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ids', nargs='+', choices=[item[0] for item in CATALOG], help='Fetch only these source IDs')
    args = parser.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    ledger = HERE / 'SOURCES.json'
    previous = {r['id']: r for r in json.loads(ledger.read_text(encoding='utf-8'))['sources']} if ledger.exists() else {}
    for sid, term in EXPECTED.items():
        old = previous.get(sid)
        extracted = RAW / (sid + '.extracted.txt')
        if old and old['status'] == 'retrieved' and extracted.exists() and term not in extracted.read_text(encoding='utf-8').lower():
            old.update(status='identity_mismatch', error='Source identity verification rejected this document')
    jobs = [item for item in CATALOG if previous.get(item[0], {}).get('status') != 'retrieved' and (not args.ids or item[0] in args.ids)]
    jobs = [(sid, title, FALLBACKS.get(sid, url), topic) for sid, title, url, topic in jobs]
    with ThreadPoolExecutor(max_workers=8) as pool:
        refreshed = list(pool.map(fetch, jobs))
    for record in refreshed:
        old = previous.get(record['id'])
        if old:
            record['previous_attempts'] = old.get('previous_attempts', []) + [{k: v for k, v in old.items() if k != 'previous_attempts'}]
        previous[record['id']] = record
    records = [previous.get(item[0], dict(id=item[0], title=item[1], url=item[2], topic=item[3], status='not_requested')) for item in CATALOG]
    ledger.write_text(json.dumps({
        'scope': 'Bounded public primary-source review, not an exhaustive literature census',
        'raw_archive': '.local-deploy/vacuum-mapping-20261115/public-sources (private, ignored)',
        'sources': records}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for r in records:
        print(r['id'], r['status'], r['title'], r.get('error', ''))


if __name__ == '__main__':
    main()
