#!/usr/bin/env python3
"""Compare held-pose captures using relative quaternions, without changing calibration."""
import argparse
import json
import math


def normalize(q):
    if len(q) != 4 or not all(math.isfinite(v) for v in q):
        raise ValueError('invalid quaternion')
    n = math.sqrt(sum(v*v for v in q))
    if not .5 < n < 1.5:
        raise ValueError('invalid quaternion norm')
    return [v/n for v in q]


def multiply(a, b):
    w,x,y,z = a
    v,i,j,k = b
    return [w*v-x*i-y*j-z*k, w*i+x*v+y*k-z*j,
            w*j-x*k+y*v+z*i, w*k+x*j-y*i+z*v]


def relative_rotation(reference, pose):
    a, b = normalize(reference), normalize(pose)
    q = normalize(multiply([a[0], -a[1], -a[2], -a[3]], b))
    if q[0] < 0:
        q = [-v for v in q]
    s = math.sqrt(sum(v*v for v in q[1:]))
    angle = 2*math.atan2(s, q[0])
    axis = [v/s for v in q[1:]] if s > 1e-9 else [0,0,0]
    return dict(angle_deg=math.degrees(angle), sensor_axis=axis,
                rotation_vector_deg=[v*math.degrees(angle) for v in axis])


def mean_pose(capture):
    # Use the last half of a capture to reduce handling motion at its start.
    qs = [normalize([r['message'][f'q{i}'] for i in range(1,5)])
          for r in capture['records']
          if r['message']['mavpackettype'] == 'ATTITUDE_QUATERNION'
          and r['received_s'] >= capture['seconds']/2]
    if len(qs) < 20:
        raise ValueError('insufficient quaternion samples')
    aligned = [q if sum(a*b for a,b in zip(q,qs[0])) >= 0 else [-v for v in q] for q in qs]
    mean = normalize([sum(q[i] for q in aligned)/len(qs) for i in range(4)])
    spread = max(relative_rotation(mean,q)['angle_deg'] for q in qs)
    if spread > 3:
        raise ValueError(f'pose was not held still: quaternion spread {spread:.2f} degrees')
    return mean, spread


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('reference')
    ap.add_argument('pose')
    args = ap.parse_args()
    with open(args.reference) as f:
        a, sa = mean_pose(json.load(f))
    with open(args.pose) as f:
        b, sb = mean_pose(json.load(f))
    print(json.dumps(relative_rotation(a,b) | dict(reference_spread_deg=sa, pose_spread_deg=sb), indent=2))


if __name__ == '__main__':
    main()
