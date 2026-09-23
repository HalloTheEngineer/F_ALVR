use alvr_common::{
    glam::Quat, parking_lot::Mutex, DeviceMotion, Pose, TelemetryLogger, unix_timestamp_ms,
};
use alvr_packets::ClientStatistics;
use alvr_session::Settings;
use serde_json::json;
use std::{path::PathBuf, sync::Arc, time::Duration};

#[derive(Clone, Copy)]
pub struct TelemetrySample {
    pub motion: DeviceMotion,
    // Revision of the pose reported by the OpenXR runtime for the same timestamp, measured one
    // input poll later. None when tracking is not valid.
    pub residual_position_m: Option<f32>,
    pub residual_orientation_deg: Option<f32>,
}

pub struct ClientTelemetry {
    base_dir: Option<PathBuf>,
    input_logger: Mutex<Option<Arc<TelemetryLogger>>>,
    frames_logger: Mutex<Option<Arc<TelemetryLogger>>>,
}

impl ClientTelemetry {
    pub fn new(base_dir: Option<PathBuf>) -> Self {
        Self {
            base_dir,
            input_logger: Mutex::new(None),
            frames_logger: Mutex::new(None),
        }
    }

    pub fn start(&self, settings: &Settings, fps: f32, eye_resolution: [u32; 2], wired: bool) {
        let Some(base_dir) = &self.base_dir else {
            return;
        };

        let Some(session_dir) = TelemetryLogger::create_session_dir(base_dir) else {
            return;
        };

        let header = json!({
            "type": "header",
            "role": "client",
            "version": alvr_common::ALVR_VERSION.to_string(),
            "unix_ms": unix_timestamp_ms(),
            "settings": {
                "codec": format!("{:?}", settings.video.preferred_codec),
                "fps": fps,
                "wired": wired,
                "max_prediction_ms": settings.headset.max_prediction_ms,
                "steamvr_pipeline_frames": settings
                    .headset
                    .controllers
                    .as_option()
                    .map(|config| config.steamvr_pipeline_frames),
                "eye_resolution": eye_resolution,
            },
        })
        .to_string();

        *self.input_logger.lock() = TelemetryLogger::start(&session_dir, "input", &header);
        *self.frames_logger.lock() = TelemetryLogger::start(&session_dir, "frames", &header);
    }

    pub fn stop(&self) {
        if let Some(logger) = self.input_logger.lock().take() {
            logger.stop();
        }
        if let Some(logger) = self.frames_logger.lock().take() {
            logger.stop();
        }
    }

    pub fn log_input_sample(
        &self,
        poll_timestamp: Duration,
        now_timestamp: Duration,
        head: &TelemetrySample,
        hands: [Option<(u64, TelemetrySample)>; 2],
    ) {
        let Some(logger) = &*self.input_logger.lock() else {
            return;
        };

        fn device_json(sample: &TelemetrySample) -> serde_json::Value {
            let DeviceMotion {
                pose,
                linear_velocity,
                angular_velocity,
            } = &sample.motion;

            json!({
                "p": [pose.position.x, pose.position.y, pose.position.z],
                "q": [
                    pose.orientation.x,
                    pose.orientation.y,
                    pose.orientation.z,
                    pose.orientation.w,
                ],
                "v": [linear_velocity.x, linear_velocity.y, linear_velocity.z],
                "av": [
                    angular_velocity.x,
                    angular_velocity.y,
                    angular_velocity.z,
                ],
                "res_pos_mm": sample
                    .residual_position_m
                    .map(|residual| residual * 1000.0),
                "res_ori_deg": sample.residual_orientation_deg,
            })
        }

        logger.log(
            json!({
                "type": "input",
                "unix_ms": unix_timestamp_ms(),
                "poll_ts_us": poll_timestamp.as_micros(),
                "now_ts_us": now_timestamp.as_micros(),
                "head": device_json(head),
                "hands": hands
                    .into_iter()
                    .flatten()
                    .map(|(id, sample)| {
                        let mut obj = device_json(&sample);
                        obj["id"] = json!(id);
                        obj
                    })
                    .collect::<Vec<_>>(),
            })
            .to_string(),
        );
    }

    pub fn log_frame(&self, stats: &ClientStatistics) {
        let Some(logger) = &*self.frames_logger.lock() else {
            return;
        };

        logger.log(
            json!({
                "type": "frame",
                "unix_ms": unix_timestamp_ms(),
                "target_ts_us": stats.target_timestamp.as_micros(),
                "frame_interval_us": stats.frame_interval.as_micros(),
                "video_decode_us": stats.video_decode.as_micros(),
                "video_decoder_queue_us": stats.video_decoder_queue.as_micros(),
                "rendering_us": stats.rendering.as_micros(),
                "vsync_queue_us": stats.vsync_queue.as_micros(),
                "total_pipeline_latency_us": stats.total_pipeline_latency.as_micros(),
            })
            .to_string(),
        );
    }
}

// Telemetry with no base directory: all logging calls become no-ops
impl Default for ClientTelemetry {
    fn default() -> Self {
        Self::new(None)
    }
}

pub fn pose_residual(last_pose: &Pose, measured_pose: &Pose) -> (f32, f32) {
    let position_residual = last_pose.position.distance(measured_pose.position);

    let orientation_residual = Quat::angle_between(
        last_pose.orientation,
        measured_pose.orientation,
    )
    .to_degrees();

    (position_residual, orientation_residual)
}
