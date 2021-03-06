#!/usr/bin/env python

import rospy
import numpy as np
import time
import utils
import tf
import math

import sys

from geometry_msgs.msg import PoseArray, PoseStamped
from visualization_msgs.msg import Marker
from ackermann_msgs.msg import AckermannDriveStamped
from nav_msgs.msg import Odometry
from std_msgs.msg import Float32

from scipy import interpolate

k = 0.1  # look forward gain
Lfc = 2.0  # [m] look-ahead distance
Kp = 1.0  # speed proportional gain
dt = 0.1  # [s] time tick
WB = 2.9  # [m] wheel base of vehicle


class PurePursuit(object):
    """
    Implements Pure Pursuit trajectory tracking with a fixed lookahead and speed.
    """

    def __init__(self):
        self.odom_topic = rospy.get_param("~odom_topic") # note: this throws an error in sim w/o particle filter running
        # self.odom_topic = "/odom"

        self.k = 0.1  # look forward gain
        self.default_lookahead_distance = 2.0
        self.min_lookahead_distance = 0.35
        self.lookahead_distance = 2.0  # lookahead distance currently being used; scaled in pure_pursuit() based on curvature of trajectory
        self.speed = 4.0  # TODO: Any changes needed? Do we need to get speed as parameter?
        self.wheelbase_length = 0.8  # TODO is this okay?
        self.Kp = 1.0  # speed proportional gain
        self.trajectory = utils.LineTrajectory("/followed_trajectory")
        self.ind = 0

        self.odom_sub = rospy.Subscriber(self.odom_topic, Odometry, self.odom_callback, queue_size=1)
        self.traj_sub = rospy.Subscriber("/trajectory/current", PoseArray, self.trajectory_callback, queue_size=1)
        self.drive_pub = rospy.Publisher("/drive", AckermannDriveStamped, queue_size=1)
        self.drive_cmd = AckermannDriveStamped()
        self.drive_cmd.drive.speed = self.speed  # TODO: do we need to subscribe to this? Or can we pick speed?
        self.point_X_pub = rospy.Publisher("/point_X", Float32, queue_size=1)
        self.point_Y_pub = rospy.Publisher("/point_y", Float32, queue_size=1)
        self.error_pub = rospy.Publisher("/car_error", Float32, queue_size=1)

        self.odom_lock = False
        self.old_nearest_point_index = None

    def pure_pursuit_steer_control(self):
        ind, Lf = self.get_target_index()

        if ind < len(self.trajectory.points):
            tx, ty = self.trajectory.points[ind]
        else:  # toward goal
            tx, ty = self.trajectory.points[-1]

        self.point_X_pub.publish(tx)
        self.point_Y_pub.publish(ty)

        alpha = math.atan2(ty - self.car_point[1], tx - self.car_point[0]) - self.car_theta

        delta = math.atan2(2.0 * self.wheelbase_length * math.sin(alpha) / Lf, 1.0)

        return delta, ind

    def get_lin_reg(self, ind):

        lin_reg = np.polyfit([self.trajectory.points[self.ind - ind][0], self.trajectory.points[self.ind - (ind - 1)][
            0]],
                             [self.trajectory.points[self.ind - ind][1], self.trajectory.points[self.ind - (ind - 1)][
                                 1]],
                             deg=1)
        return lin_reg

    def calc_dist(self, lin_reg):
        a = -lin_reg[0]
        b = 1
        c = -lin_reg[1]
        car_error = abs(a * self.car_point[0] + b * self.car_point[1] + c) / (np.sqrt(a ** 2 + b ** 2))
        return car_error

    def get_target_index(self):
        # To speed up nearest point search, doing it at only first time.
        while len(self.trajectory.points) == 0:
            rospy.loginfo("Waiting for trajectory points...")
            time.sleep(2)

        if self.old_nearest_point_index is None:
            # search nearest point index
            dx = [self.car_point[0] - pt[0] for pt in self.trajectory.points]
            dy = [self.car_point[1] - pt[1] for pt in self.trajectory.points]
            d = np.hypot(dx, dy)
            ind = np.argmin(d)
            self.old_nearest_point_index = ind
        else:
            ind = self.old_nearest_point_index
            if ind >= len(self.trajectory.points):
                return
            distance_this_index = self.calc_distance_from_car(self.trajectory.points[ind])
            # rospy.loginfo("IND: " + str(ind))
            # rospy.loginfo(len(self.trajectory.points))

            while True:

                if ind + 1 <= len(self.trajectory.points) - 1:
                    distance_next_index = self.calc_distance_from_car(self.trajectory.points[ind + 1])
                else:
                    distance = self.calc_distance_from_car(self.trajectory.points[-1])
                    time.sleep(distance / self.speed * 2)
                    self.drive_cmd.drive.speed = 0
                    self.drive_pub.publish(self.drive_cmd)
                    # print("Reached end of trajectory!")

                if distance_this_index < distance_next_index:
                    break

                ind = ind + 1 if (ind + 1) < len(self.trajectory.points) else ind
                distance_this_index = distance_next_index
            self.old_nearest_point_index = ind

        curvature = self.curvature(ind, min(ind + 1, len(self.trajectory.points) - 1),
                                   min(ind + 2, len(self.trajectory.points) - 1))
        Lf = self.k * self.speed + self.lookahead_distance  # update look ahead distance

        # search look ahead target point index
        while Lf > self.calc_distance_from_car(self.trajectory.points[ind]):
            if (ind + 1) >= len(self.trajectory.points) - 1:
                break  # not exceed goal
            ind += 1

        self.ind = ind
        if ind > 21:
            ind_list = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
            lin_reg_list = map(self.get_lin_reg, ind_list)
            error_list = map(self.calc_dist, lin_reg_list)
            car_error = min(error_list)
        else:
            lin_reg_next = np.polyfit([self.trajectory.points[ind][0], self.trajectory.points[ind + 1][0]],
                                      [self.trajectory.points[ind][1], self.trajectory.points[ind + 1][1]], deg=1)

            if ind > 0:
                lin_reg_prev = np.polyfit([self.trajectory.points[ind - 1][0], self.trajectory.points[ind][0]],
                                          [self.trajectory.points[ind - 1][1], self.trajectory.points[ind][1]], deg=1)
            else:
                lin_reg_prev = lin_reg_next

            if ind > 1:
                lin_reg_prev_prev = np.polyfit([self.trajectory.points[ind - 2][0], self.trajectory.points[ind - 1][0]],
                                               [self.trajectory.points[ind - 2][1], self.trajectory.points[ind - 1][1]],
                                               deg=1)

            else:
                lin_reg_prev_prev = lin_reg_next

            if ind > 2:
                lin_reg_prev_prev = np.polyfit([self.trajectory.points[ind - 3][0], self.trajectory.points[ind - 2][0]],
                                               [self.trajectory.points[ind - 3][1], self.trajectory.points[ind - 2][1]],
                                               deg=1)

            a = -lin_reg_next[0]
            b = 1
            c = -lin_reg_next[1]

            a2 = -lin_reg_prev[0]
            c2 = -lin_reg_prev[1]

            a3 = -lin_reg_prev_prev[0]
            c3 = -lin_reg_prev_prev[1]

            a4 = -lin_reg_prev_prev[0]
            c4 = -lin_reg_prev_prev[1]

            car_error = min(abs(a * self.car_point[0] + b * self.car_point[1] + c) / (np.sqrt(a ** 2 + b ** 2)),
                            abs(a2 * self.car_point[0] + b * self.car_point[1] + c2) / (np.sqrt(a2 ** 2 + b ** 2)),
                            abs(a3 * self.car_point[0] + b * self.car_point[1] + c3) / (np.sqrt(a3 ** 2 + b ** 2)),
                            abs(a4 * self.car_point[0] + b * self.car_point[1] + c4) / (np.sqrt(a4 ** 2 + b ** 2)))

        self.error_pub.publish(car_error)

        # get distance of current line segment so we can scale lookahead distance by it

        # if 0 < ind < len(self.trajectory.points):
        # #rospy.loginfo("LPOJITYURSDFXGUIJKOJTYUFRDYFGIJOPKJITYUFDR")
        # #rospy.loginfo(len(self.trajectory.points))
        self.trajectory.update_distances()

        if ind == 1 or ind == 0:
            dist = self.trajectory.distance_along_trajectory(ind)

        else:
            dist1 = self.trajectory.distance_along_trajectory(ind)
            dist2 = self.trajectory.distance_along_trajectory(ind - 1)
            dist = dist1 - dist2

        if ind == 0:
            self.lookahead_distance = self.default_lookahead_distance

        elif dist < self.default_lookahead_distance:
            self.lookahead_distance = dist / self.default_lookahead_distance * self.k * self.speed
            if self.lookahead_distance < self.min_lookahead_distance:
                self.lookahead_distance = self.min_lookahead_distance
        else:
            self.lookahead_distance = self.default_lookahead_distance

        # rospy.loginfo(self.lookahead_distance)
        return ind, Lf

    def calc_distance_from_car(self, pt):
        dx = self.car_point[0] - pt[0]
        dy = self.car_point[1] - pt[1]
        return math.hypot(dx, dy)

    # def in_front_of_car(self, ind):
    #     theta = np.arctan2(ind[1] - self.car_point[1], ind[0] - self.car_point[0]) - self.car_theta
    #     if theta > np.pi:
    #         theta = -2.0 * np.pi + theta
    #     elif theta < -np.pi:
    #         theta = 2.0 * np.pi + theta
    #
    #     return theta > -np.pi/2.0 or theta < np.pi/2.0

    def curvature(self, ind1, ind2, ind3):
        """
        Returns curvature of line calculated from the three points on the trajectory currently in use
        """
        pt1, pt2, pt3 = self.trajectory.points[ind1], self.trajectory.points[ind2], self.trajectory.points[ind3]
        x1 = pt1[0]
        x2 = pt2[0]
        x3 = pt3[0]
        y1 = pt1[1]
        y2 = pt2[1]
        y3 = pt3[1]

        ab = ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** (1 / 2)
        ac = ((x1 - x3) ** 2 + (y1 - y3) ** 2) ** (1 / 2)
        bc = ((x2 - x3) ** 2 + (y2 - y3) ** 2) ** (1 / 2)

        area = 1 / 2 * (ac) * ((ab) ** 2 - (ac / 2) ** 2) ** (1 / 2)

        curvature = 4 * area / (ab * ac * bc)
        return curvature

    def trajectory_callback(self, msg):
        """
        Clears the currently followed trajectory, and loads the new one from the message
        """
        # print "Receiving new trajectory:", len(msg.poses), "points"
        self.trajectory.clear()
        self.trajectory.fromPoseArray(msg)
        self.trajectory.publish_viz(duration=0.0)

        # rospy.loginfo("Trajectory callback")

        def multiInterp2(x, xp, fp):
            i = np.arange(x.size)
            j = np.searchsorted(xp, x) - 1
            d = (x - xp[j]) / (xp[j + 1] - xp[j])
            return (1 - d) * fp[i, j] + fp[i, j + 1] * d

        t = np.arange(0, len(self.trajectory.points))
        x = [pt[0] for pt in self.trajectory.points]
        y = [pt[1] for pt in self.trajectory.points]

        f_x = interpolate.interp1d(t, x)
        f_y = interpolate.interp1d(t, y)

        new_t = np.arange(0, len(self.trajectory.points) - 1, 0.1)
        self.trajectory.points = [(new_x, new_y) for (new_x, new_y) in zip(f_x(new_t), f_y(new_t))]

        self.old_nearest_point_index = None
        self.odom_lock = False

        self.old_nearest_point_index = None

    def odom_callback(self, msg):
        # rospy.loginfo("odom callback called")
        if not self.odom_lock:
            # #rospy.loginfo("ODOM CALLBACK -------------------")
            x = msg.pose.pose.position.x
            y = msg.pose.pose.position.y
            # #rospy.loginfo("CAR POSITION ---------------")
            # #rospy.loginfo(x)
            # #rospy.loginfo(y)
            quat = msg.pose.pose.orientation
            orientation = tf.transformations.euler_from_quaternion(np.array([quat.x, quat.y, quat.z, quat.w]))
            theta = orientation[2]
            # rospy.loginfo("odom callback unlocked")
            self.car_theta = theta
            self.car_point = (x, y)
            di, target_ind = self.pure_pursuit_steer_control()
            # rospy.loginfo(target_ind)
            if target_ind >= len(self.trajectory.points) - 1 and len(self.trajectory.points) != 0:
                self.drive_cmd.drive.speed = 0
                self.odom_lock = True
            else:
                self.drive_cmd.drive.speed = self.speed
                self.drive_cmd.drive.steering_angle = di
            self.drive_pub.publish(self.drive_cmd)


if __name__ == "__main__":
    rospy.init_node("pure_pursuit")
    pf = PurePursuit()
    rospy.spin()
