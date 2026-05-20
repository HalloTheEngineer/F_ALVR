use super::schema::{HigherOrderChoiceSchema, PresetModifierOperation};
use crate::dashboard::components::{self, NestingInfo, SettingControl};
use alvr_packets::{PathSegment, PathValuePair};
use eframe::egui::Ui;
use serde_json as json;
use settings_schema::{SchemaEntry, SchemaNode};
use std::collections::{HashMap, HashSet};

pub struct Control {
    name: String,
    modifiers: HashMap<String, Vec<PathValuePair>>,
    control: SettingControl,
    preset_json: json::Value,
}

impl Control {
    pub fn new(schema: HigherOrderChoiceSchema) -> Self {
        let modifiers = schema
            .options
            .iter()
            .map(|option| {
                (
                    option.display_name.clone(),
                    option
                        .modifiers
                        .iter()
                        .map(|modifier| match &modifier.operation {
                            PresetModifierOperation::Assign(value) => PathValuePair {
                                path: alvr_packets::parse_path(&modifier.target_path),
                                value: value.clone(),
                            },
                        })
                        .collect(),
                )
            })
            .collect();

        let mut strings = schema.strings;
        strings.insert("display_name".into(), schema.name.clone());

        let control_schema = SchemaNode::Section {
            entries: vec![SchemaEntry {
                name: schema.name.clone(),
                strings,
                flags: schema.flags,
                content: SchemaNode::Choice {
                    default: schema
                        .options
                        .iter()
                        .find(|option| option.display_name == schema.default_option_display_name)
                        .unwrap()
                        .display_name
                        .clone(),
                    variants: schema
                        .options
                        .into_iter()
                        .map(|option| SchemaEntry {
                            name: option.display_name.clone(),
                            strings: [("display_name".into(), option.display_name)]
                                .into_iter()
                                .collect(),
                            flags: HashSet::new(),
                            content: None,
                        })
                        .collect(),
                    gui: Some(schema.gui),
                },
            }],
            gui_collapsible: false,
        };

        let control = SettingControl::new(
            NestingInfo {
                path: vec![],
                indentation_level: 0,
            },
            control_schema,
        );

        let preset_json = json::json!({ {&schema.name}: { "variant": "" } });

        Self {
            name: schema.name,
            modifiers,
            control,
            preset_json,
        }
    }

    pub fn update_session_settings(&mut self, session_setting_json: &json::Value) {
        let mut selected_option = String::new();

        // First pass: try presets with non-empty modifiers (exact matches)
        for (key, descs) in &self.modifiers {
            if descs.is_empty() {
                continue;
            }
            if Self::matches_all(session_setting_json, descs) {
                selected_option.clone_from(key);
                break;
            }
        }

        // Second pass: fall back to empty-modifier presets (e.g. "Custom")
        if selected_option.is_empty() {
            for (key, descs) in &self.modifiers {
                if descs.is_empty() {
                    selected_option.clone_from(key);
                    break;
                }
            }
        }

        self.preset_json[&self.name]["variant"] = json::Value::String(selected_option);
    }

    fn matches_all(session_setting_json: &json::Value, descs: &[PathValuePair]) -> bool {
        for desc in descs {
            let mut session_ref = session_setting_json;

            // Note: the first path segment is always "settings_schema". Skip that.
            for segment in &desc.path[1..] {
                session_ref = match segment {
                    PathSegment::Name(name) => {
                        if let Some(name) = session_ref.get(name) {
                            name
                        } else {
                            return false;
                        }
                    }
                    PathSegment::Index(index) => {
                        if let Some(index) = session_ref.get(index) {
                            index
                        } else {
                            return false;
                        }
                    }
                };
            }

            if !components::json_values_eq(session_ref, &desc.value) {
                return false;
            }
        }

        true
    }

    pub fn ui(&mut self, ui: &mut Ui) -> Vec<PathValuePair> {
        if let Some(desc) = self.control.ui(ui, &mut self.preset_json, false) {
            // todo: handle children requests
            self.modifiers[desc.value.as_str().unwrap()].clone()
        } else {
            vec![]
        }
    }
}
