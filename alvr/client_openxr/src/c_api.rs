#[cfg(target_os = "android")]
#[unsafe(no_mangle)]
pub extern "C" fn alvr_entry_point(java_vm: *mut std::ffi::c_void, context: *mut std::ffi::c_void) {
    unsafe { ndk_context::initialize_android_context(java_vm, context) };

    // Telemetry requires the Android app object to resolve the external data dir, which is not
    // available through this entry point
    crate::entry_point(None);
}
