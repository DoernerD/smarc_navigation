#!/usr/bin/python3

import rospy
import numpy as np
import tf
from smarc_msgs.msg import DVL, ThrusterFeedback
from geometry_msgs.msg import PointStamped, TransformStamped, Quaternion, PoseWithCovarianceStamped
import tf
import tf2_ros
from tf.transformations import euler_from_quaternion, quaternion_from_euler, quaternion_multiply
import message_filters
from sensor_msgs.msg import Imu
from nav_msgs.msg import Odometry
from std_srvs.srv import SetBool, SetBoolRequest, SetBoolRequest
# from sbg_driver.msg import SbgEkfEuler
# from floatsam_mm import *
# from sam_msgs.msg import ThrusterAngles
from sbg_driver.msg import SbgEkfQuat


class VehicleDR(object):

    def __init__(self):
        self.odom_top = rospy.get_param('~odom_topic', '/sam/dr/dvl_dr')
        self.stim_topic = rospy.get_param('~imu', '/sam/core/imu')
        self.sbg_topic = rospy.get_param('~sbg_topic', '/sam/core/imu')
        self.base_frame = rospy.get_param('~base_frame', 'sam/base_link')
        self.base_frame_2d = rospy.get_param('~base_frame_2d', 'sam/base_link')
        self.odom_frame = rospy.get_param('~odom_frame', 'sam/odom')
        self.map_frame = rospy.get_param('~map_frame', 'map')
        self.utm_frame = rospy.get_param('~utm_frame', 'utm')
        # self.dvl_topic = rospy.get_param('~dvl_topic', '/sam/core/dvl')
        # self.dvl_frame = rospy.get_param('~dvl_frame', 'dvl_link')
        # self.dvl_period = rospy.get_param('~dvl_period', 0.2)
        self.dr_period = rospy.get_param('~dr_period', 0.02)
        self.rpm1_topic = rospy.get_param('~thrust1_fb', '/sam/core/rpm_fb1')
        self.rpm2_topic = rospy.get_param('~thrust2_fb', '/sam/core/rpm_fb2')
        self.thrust_topic = rospy.get_param('~thrust_vec_cmd', '/sam/core/thrust')
        self.gps_topic = rospy.get_param('~gps_odom_topic', '/sam/core/gps')
        self.KT = rospy.get_param('~KT_thrusters', 0.1)
        # self.dr_pub_period = rospy.get_param('~dr_pub_period', 0.1)

        self.listener = tf.TransformListener()
        self.static_tf_bc = tf2_ros.StaticTransformBroadcaster()
        self.br = tf.TransformBroadcaster()
        self.transformStamped = TransformStamped()

        self.t_prev = rospy.Time.now()
        self.pose_prev = [0.] * 6
        self.init_heading = False
        self.init_m2o = False
        
        # Stim integration
        self.rot_t = [0.] * 3
        self.t_stim_prev = 0.
        self.init_stim = False
        self.vel_rot = [0.] * 3

        # Useful when working with rosbags
        self.t_start = 0.   
        self.t_now = 0. 
        self.t_pub = 0. 

        # # DVL integration
        # self.pos_t = [0.] * 3
        # self.t_dvl_prev = 0.
        # self.dvl_on = False
        # self.dvl_latest = DVL()
        # self.dvl_latest.velocity.x = 0.
        # self.dvl_latest.velocity.y = 0.
        # self.dvl_latest.velocity.z = 0.

        # Depth measurements: init to zero
        self.b2p_trans = [0.] * 3
        self.depth_meas = False # If no press sensor available, assume surface vehicle
        self.base_depth = 0. # abs depth of base frame

        # Motion model
        # self.floatsam = FloatSAM() 
        # self.mm_on = False
        # self.mm_linear_vel = [0.] * 3
        # dr = np.clip(0., -7 * np.pi / 180, 7 * np.pi / 180)        
        self.u = np.array([0., 0.])

        # Connect
        self.pub_odom = rospy.Publisher(self.odom_top, Odometry, queue_size=100)
        #self.sbg_sub = rospy.Subscriber(self.sbg_topic, SbgEkfQuat, self.sbg_cb)
        self.sbg_sub = rospy.Subscriber(self.sbg_topic, Imu, self.sbg_cb,  queue_size=10)
        # self.dvl_sub = rospy.Subscriber(self.dvl_topic, DVL, self.dvl_cb)
        self.stim_sub = rospy.Subscriber(self.stim_topic, Imu, self.stim_cb, queue_size=10)
        # self.depth_sub = rospy.Subscriber(self.depth_top, PoseWithCovarianceStamped, self.depth_cb)
        self.gps_sub = rospy.Subscriber(self.gps_topic, Odometry, self.gps_cb)

        self.thrust1_sub = message_filters.Subscriber(self.rpm1_topic, ThrusterFeedback)
        self.thrust2_sub = message_filters.Subscriber(self.rpm2_topic, ThrusterFeedback)
        self.ts = message_filters.ApproximateTimeSynchronizer([self.thrust1_sub, self.thrust2_sub],
                                                                20, slop=20.0, allow_headerless=False)
        self.ts.registerCallback(self.thrust_cb)

        rospy.Timer(rospy.Duration(self.dr_period), self.dr_timer)

        rospy.spin()


    def gps_cb(self, gps_msg):
        try:
            # goal_point_local = self.listener.transformPoint("map", goal_point)
            (world_trans, world_rot) = self.listener.lookupTransform(self.map_frame, self.odom_frame, rospy.Time(0))

        except (tf.LookupException, tf.ConnectivityException):

            goal_point = PointStamped()
            goal_point.header.frame_id = self.utm_frame
            goal_point.header.stamp = rospy.Time(0)
            goal_point.point.x = gps_msg.pose.pose.position.x
            goal_point.point.y = gps_msg.pose.pose.position.y
            goal_point.point.z = 0.

            try:
                gps_map = self.listener.transformPoint(self.map_frame, goal_point)

                if self.init_heading:
                    rospy.loginfo("DR node: broadcasting transform %s to %s" % (self.map_frame, self.odom_frame))            
                    
                    euler = euler_from_quaternion([self.init_quat.x, self.init_quat.y, self.init_quat.z, self.init_quat.w])
                    quat = quaternion_from_euler(0.,0., euler[2]) # -0.3 for feb_24 with floatsam
                    
                    # -0.3 for feb_24 with floatsam
                    #quat = quaternion_from_euler(0., 0., self.init_yaw + np.pi/2)
                    
                    self.transformStamped.transform.translation.x = gps_map.point.x
                    #self.transformStamped.transform.translation.x = 0.
                    self.transformStamped.transform.translation.y = gps_map.point.y
                    #self.transformStamped.transform.translation.y = 0.
                    self.transformStamped.transform.translation.z = 0.
                    self.transformStamped.transform.rotation = Quaternion(*quat)
                    self.transformStamped.header.frame_id = self.map_frame
                    self.transformStamped.child_frame_id = self.odom_frame
                    self.transformStamped.header.stamp = rospy.Time.now()
                    self.static_tf_bc.sendTransform(self.transformStamped)
                    self.init_m2o = True
                    self.gps_sub.unregister()

            except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                rospy.logwarn("DR: Transform to utm-->map not available yet")
            pass


    def dr_timer(self, event):
        
        if self.init_m2o and self.init_stim:

            pose_t = np.concatenate([self.pos_t, self.rot_t])    # Catch latest estimate from IMU
            rot_vel_t = self.vel_rot    # TODO: rn this keeps the last vels even if the IMU dies
            
            # Linear vels from diff drive
            lin_vel_t = np.zeros(3)
            lin_vel_t[0:2] = np.matmul(np.matrix([[np.cos(pose_t[5]), 0], 
                                                  [0, np.sin(pose_t[5])]]), 0.5 * self.KT * self.u)
            
            # Integrate linear vels                    
            rot_mat_t = self.fullRotation(pose_t[3], pose_t[4], pose_t[5])
            step_t = np.matmul(rot_mat_t, lin_vel_t * self.dr_period)
            pose_t[0:2] += step_t[0:2]

            # Measure depth directly
            pose_t[2] = self.base_depth

            # Publish and broadcast aux frame for testing
            quat_t = tf.transformations.quaternion_from_euler(pose_t[3],pose_t[4],pose_t[5])
            odom_msg = Odometry()
            odom_msg.header.frame_id = self.odom_frame
            odom_msg.header.stamp = rospy.Time.now()
            # odom_msg.child_frame_id = self.base_frame
            odom_msg.child_frame_id = "base_test"
            odom_msg.pose.pose.position.x = pose_t[0]
            odom_msg.pose.pose.position.y = pose_t[1]
            odom_msg.pose.pose.position.z = pose_t[2]
            odom_msg.twist.twist.linear.x = lin_vel_t[0]
            odom_msg.twist.twist.linear.y = lin_vel_t[1]
            odom_msg.twist.twist.linear.z = lin_vel_t[2]
            odom_msg.twist.twist.angular.x = rot_vel_t[0]
            odom_msg.twist.twist.angular.y = rot_vel_t[1]
            odom_msg.twist.twist.angular.z = rot_vel_t[2]
            odom_msg.pose.covariance = [0.] * 36
            odom_msg.pose.pose.orientation = Quaternion(*quat_t)
            self.pub_odom.publish(odom_msg)

            # Base link frame 
            self.br.sendTransform([pose_t[0], pose_t[1], pose_t[2]],
                        quat_t,
                        rospy.Time.now(),
                        # self.base_frame,
                        "base_test",
                        self.odom_frame)
            
            quat_t = tf.transformations.quaternion_from_euler(0., 0., pose_t[5])
            self.br.sendTransform([pose_t[0], pose_t[1], pose_t[2]],
                        quat_t,
                        rospy.Time.now(),
                        self.base_frame_2d,
                        self.odom_frame)
            
            self.t_now += self.dr_period

            # Update global variable
            self.pos_t = pose_t[0:3]

    def thrust_cb(self, thrust1_msg, thrust2_msg):

        thrust = thrust1_msg.rpm.rpm + thrust2_msg.rpm.rpm
        self.u = np.array([thrust, thrust])



    def sbg_cb(self, sbg_msg):
        self.init_quat = sbg_msg.orientation
        self.init_heading = True
        
        #self.init_quat = sbg_msg.quaternion
        
        #if not self.init_heading:
        #    self.init_heading = True
        #    self.init_yaw = euler_from_quaternion(
        #        [self.init_quat.y, self.init_quat.x, -self.init_quat.z, self.init_quat.w])[2]
        #else:
        #    self.rot_sbg = np.array([sbg_msg.quaternion.y,
        #                             sbg_msg.quaternion.x,
        #                             -sbg_msg.quaternion.z,
        #                             sbg_msg.quaternion.w])
        #    self.rot_t[2] = tf.transformations.euler_from_quaternion(
        #        self.rot_sbg)[2] - self.init_yaw


    def fullRotation(self, roll, pitch, yaw):
        rot_z = np.array([[np.cos(yaw), -np.sin(yaw), 0.0],
                          [np.sin(yaw), np.cos(yaw), 0.0],
                          [0., 0., 1]])
        rot_y = np.array([[np.cos(pitch), 0.0, np.sin(pitch)],
                          [0., 1., 0.],
                          [-np.sin(pitch), np.cos(pitch), 0.0]])
        rot_x = np.array([[1., 0., 0.],
                          [0., np.cos(roll), -np.sin(roll)],
                          [0., np.sin(roll), np.cos(roll)]])

        return np.matmul(rot_z, np.matmul(rot_y, rot_x))


    def stim_cb(self, imu_msg):
        if self.init_stim and self.init_m2o:
            self.rot_stim = np.array([imu_msg.orientation.x,
                                    imu_msg.orientation.y,
                                    imu_msg.orientation.z,
                                    imu_msg.orientation.w])
            euler_t = tf.transformations.euler_from_quaternion(self.rot_stim)

            # Integrate yaw velocities
            self.vel_rot = np.array([imu_msg.angular_velocity.x,
                                imu_msg.angular_velocity.y,
                                imu_msg.angular_velocity.z])

            dt = imu_msg.header.stamp.to_sec() - self.t_stim_prev
            self.rot_t = np.array(self.rot_t) + self.vel_rot * dt
            self.t_stim_prev = imu_msg.header.stamp.to_sec()
            
            for rot in self.rot_t:
                rot = (rot + np.pi) % (2 * np.pi) - np.pi

            # Measure roll and pitch directly
            self.rot_t[0] = euler_t[0]
            self.rot_t[1] = euler_t[1]

        else:
            # rospy.loginfo("Stim data coming in")
            self.t_stim_prev = imu_msg.header.stamp.to_sec()
            self.init_stim = True


if __name__ == "__main__":
    rospy.init_node('dr_node')
    try:
        VehicleDR()
    except rospy.ROSInterruptException:
        pass