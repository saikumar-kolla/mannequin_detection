import threading
import rospy
from sensor_msgs.msg import Image, PointCloud2
import sensor_msgs.point_cloud2 as pc2
from sensor_msgs.msg import PointField
from cv_bridge import CvBridge
from ouster.sdk import client
import std_msgs.msg
from std_msgs.msg import Header
import json
import numpy as np
import cv2
from ultralytics import YOLO
import time
import open3d as o3d

class HandSegmentation:
    def __init__(self):
        self.bridge = CvBridge()
        self.model = YOLO('/home/kolla/Downloads/yolo11x-pose.pt')

        # Subscribe to image, range image, and point cloud topics from rosbag
        self.image_sub = rospy.Subscriber("/ouster/reflec_image", Image, self.image_callback)
        self.pcl_sub = rospy.Subscriber("/ouster/points", PointCloud2, self.store_pcl)
        self.range_image_sub = rospy.Subscriber("/ouster/range_image", Image, self.range_image_callback)
        self.metadata_sub = rospy.Subscriber("/ouster/metadata", std_msgs.msg.String, self.metadata_callback)

        # Publisher for segmented hand point cloud
        self.hand_pub = rospy.Publisher("/filtered_hand_points", PointCloud2, queue_size=1)

        # Store latest point cloud and range image
        self.latest_pcl = None
        self.latest_range_image = None

        # Store sensor metadata
        self.sensor_info = None
        self.metadata = None
        self.xyzlut = None  # XYZ lookup table

        # Timeout parameters for metadata
        self.metadata_received = False
        self.metadata_timeout = 30
        self.metadata_start_time = time.time()

        # Left arm keypoint indices (shoulder, elbow, wrist)
        self.left_hand_indices = [5, 7, 9]

        # Counter for logging frequency
        self.log_counter = 0

    def metadata_callback(self, msg):
        """Process metadata from /ouster/metadata"""
        try:
            self.metadata = json.loads(msg.data)
            metadata_str = json.dumps(self.metadata)
            self.sensor_info = client.SensorInfo(metadata_str)
            self.xyzlut = client.XYZLut(self.sensor_info)
            self.metadata_received = True
            rospy.loginfo("Sensor metadata loaded successfully.")
        except Exception as e:
            rospy.logerr(f"Error processing metadata: {e}")

    def store_pcl(self, pcl_msg):
        """Store the latest point cloud message"""
        self.latest_pcl = pcl_msg

    def range_image_callback(self, msg):
        try:
            range_image = self.bridge.imgmsg_to_cv2(msg, desired_encoding='passthrough')
            self.latest_range_image = range_image*4
            self.log_counter += 1
            if self.log_counter % 10 == 0:
                rospy.loginfo(f"Received range image of shape: {range_image.shape}")

        except Exception as e:
            rospy.logerr(f"Error processing range image: {e}")

    def visualize_keypoints(self, frame, keypoints):
        for i, (x, y, confidence) in enumerate(keypoints):
            if confidence > 0.5:  # Only consider keypoints with high confidence
                cv2.circle(frame, (int(x), int(y)), 3, (0, 255, 0), -1)
                cv2.putText(frame, str(i), (int(x), int(y)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 2)
        cv2.imshow("Keypoints", frame)
        cv2.waitKey(1)  # Use waitKey(1) to avoid blocking

    def image_callback(self, msg):
        try:
            frame = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
            rospy.loginfo(f"Received image of shape: {frame.shape}")

            results = self.model(frame, show=False)
            for result in results:
                if hasattr(result, 'keypoints') and result.keypoints is not None:
                    keypoints = result.keypoints.data[0].cpu().numpy()
                    rospy.loginfo(f"Keypoints shape: {keypoints.shape}")

                    if keypoints.shape[0] == 0:
                        rospy.logwarn("No keypoints detected in this frame.")
                        continue

                    mask= self.get_hand_region_mask(keypoints, frame.shape[:2])
                    if mask is None:
                        rospy.logwarn("Failed to create hand region mask.")
                        continue

                    rospy.loginfo(f"Mask shape: ,{mask.shape}")

                    if self.latest_pcl is not None and self.latest_range_image is not None:
                        self.extract_filtered_points(mask, self.latest_range_image, self.latest_pcl)

                    else:
                        rospy.logwarn("No point cloud or range image data available.")

        except Exception as e:
            rospy.logerr(f"Error processing image: {e}")
            rospy.logerr(f"Error details: {type(e).__name__}, {str(e)}")

    def get_hand_region_mask(self, keypoints, image_shape):
        try:
            shoulder_x, shoulder_y = keypoints[5, :2]
            elbow_x, elbow_y = keypoints[7, :2]
            wrist_x, wrist_y = keypoints[9, :2]
            confidences = keypoints[[5, 7, 9], 2]

            rospy.loginfo(f"Shoulder: ({shoulder_x}, {shoulder_y}), Elbow: ({elbow_x}, {elbow_y}), Wrist: ({wrist_x}, {wrist_y})")

            if np.all(confidences > 0.5):
                mask = np.zeros(image_shape, dtype=np.uint8)  # Change dtype to uint8
                radius = 1  # Radius in pixels (adjust as needed)

                # Draw cylinder around shoulder to elbow
                self.draw_cylinder(mask, shoulder_x, shoulder_y, elbow_x, elbow_y, radius)
                # Draw cylinder around elbow to wrist
                self.draw_cylinder(mask, elbow_x, elbow_y, wrist_x, wrist_y, radius)

                rospy.loginfo(f"Created mask with shape {mask.shape}")
                self.visualize_mask(mask)  # Visualize the mask
                return mask
            else:
                rospy.logwarn("Low confidence in hand keypoints.")
                return None, None
        except Exception as e:
            rospy.logerr(f"Error in get_hand_region_mask: {e}")
            return None, None

    def draw_cylinder(self, mask, x1, y1, x2, y2, radius):
        # Draw a line between (x1, y1) and (x2, y2)
        line_length = int(np.hypot(x2 - x1, y2 - y1))
        for i in range(line_length):
            t = i / line_length
            x = int(x1 * (1 - t) + x2 * t)
            y = int(y1 * (1 - t) + y2 * t)
            cv2.circle(mask, (x, y), radius, 1, -1)

    def visualize_mask(self, mask):
        # Convert mask to a three-channel image with color
        mask_visual = np.zeros((mask.shape[0], mask.shape[1], 3), dtype=np.uint8)
        mask_visual[mask > 0] = [0, 0, 255]  # Red color for the mask
        cv2.imshow("Hand Mask", mask_visual)
        cv2.waitKey(1)  # Use waitKey(1) to avoid blocking

    def extract_filtered_points(self, mask, range_image, pcl_msg):
        if not self.sensor_info or not self.xyzlut:
            rospy.logwarn("Metadata or lookup table not ready.")
            return

        try:
            #Step 1: Destagger the range image first
            destaggered_image = client.destagger(self.sensor_info, range_image, inverse=True)

            # Step 2: Apply the mask to the destaggered range image
            mask_bool = mask.astype(bool)
            roi_range_image = np.where(mask_bool, destaggered_image, np.nan)

            # Step 3: Convert the masked range image to XYZ points
            xyz_points = self.xyzlut(roi_range_image).reshape(-1, 3)

            # Step 4: Remove NaN values (invalid points) before publishing
            valid_points = xyz_points[~np.isnan(xyz_points).any(axis=1)]

            rospy.loginfo(f"Publishing {len(valid_points)} valid XYZ points.")
            fields = [
                PointField('x', 0, PointField.FLOAT32, 1),
                PointField('y', 4, PointField.FLOAT32, 1),
                PointField('z', 8, PointField.FLOAT32, 1),
            ]
            header = Header(stamp=pcl_msg.header.stamp, frame_id="os_sensor")
            pc_msg = pc2.create_cloud(header, fields, valid_points)
            self.hand_pub.publish(pc_msg)
            rospy.loginfo("Filtered point cloud published successfully.")

        except Exception as e:
            rospy.logerr(f"Error filtering points: {e}")


def ros_thread():
    HandSegmentation()
    rospy.spin()

if __name__ == '__main__':
    rospy.init_node('hand_segmentation_node')
    ros_thread = threading.Thread(target=ros_thread)
    ros_thread.start()
    ros_thread.join()