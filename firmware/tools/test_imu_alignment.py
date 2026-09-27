import math
import unittest
from imu_alignment_compare import multiply, relative_rotation, mean_pose


class AlignmentTests(unittest.TestCase):
    def test_relative_axes_with_nonlevel_reference(self):
        reference = multiply([math.cos(.2),math.sin(.2),0,0],
                             [math.cos(.3),0,math.sin(.3),0])
        for i in range(3):
            rotation = [math.cos(.15),0,0,0]
            rotation[i+1] = math.sin(.15)
            result = relative_rotation(reference, multiply(reference,rotation))
            self.assertAlmostEqual(result['angle_deg'],math.degrees(.3))
            for j, value in enumerate(result['sensor_axis']):
                self.assertAlmostEqual(value,1 if j==i else 0)

    def test_quaternion_sign_does_not_change_rotation(self):
        q=[math.cos(.2),0,0,math.sin(.2)]
        result=relative_rotation(q,[-v for v in q])
        self.assertAlmostEqual(result['angle_deg'],0)

    def test_moving_capture_rejected(self):
        records=[]
        for i in range(40):
            angle=0 if i<20 else .2
            q=[math.cos(angle),math.sin(angle),0,0]
            records.append({'received_s':6,'message':dict(mavpackettype='ATTITUDE_QUATERNION',
                            **{f'q{n+1}':v for n,v in enumerate(q)})})
        with self.assertRaises(ValueError):
            mean_pose({'seconds':8,'records':records})


if __name__ == '__main__':
    unittest.main()
