package com.akosidave.revolver

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import org.json.JSONObject

/** Server UI: start/stop and monitor. Closing it does NOT stop the server. */
class MainActivity : Activity() {

    private lateinit var status: TextView
    private lateinit var toggle: Button
    private lateinit var details: TextView
    private lateinit var keysLine: TextView
    private val ui = Handler(Looper.getMainLooper())
    private var info = JSONObject()

    private val tick = object : Runnable {
        override fun run() {
            refresh()
            ui.postDelayed(this, 2000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }

        val pad = (16 * resources.displayMetrics.density).toInt()
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        fun label(size: Float, bold: Boolean = false) = TextView(this).apply {
            textSize = size
            if (bold) setTypeface(typeface, Typeface.BOLD)
            setPadding(0, pad / 2, 0, pad / 2)
        }
        fun btn(caption: String, onClick: () -> Unit) = Button(this).apply {
            text = caption
            setOnClickListener { onClick() }
        }

        col.addView(label(22f, true).apply { text = "Revolver Server" })
        status = label(18f, true)
        col.addView(status)
        toggle = btn("Start server") { toggleServer() }
        col.addView(toggle)
        details = label(14f).apply {
            setTextIsSelectable(true)
            typeface = Typeface.MONOSPACE
        }
        col.addView(details)
        col.addView(btn("Copy token") { copy("token", info.optString("token")) })
        col.addView(btn("Copy Phone B address") { copy("address", phoneBUrl()) })
        col.addView(btn("Open chat") { openPage("/") })
        col.addView(btn("Open dashboard") { openPage("/dashboard") })
        keysLine = label(15f)
        col.addView(keysLine)
        col.addView(btn("Add Groq keys") { addKeysDialog() })
        col.addView(btn("Allow running in background") { askBattery() })

        setContentView(ScrollView(this).apply { addView(col) })
    }

    override fun onResume() {
        super.onResume()
        ui.post(tick)
    }

    override fun onPause() {
        ui.removeCallbacks(tick)
        super.onPause()
    }

    private fun refresh() {
        info = try {
            JSONObject(Py.call(this, "info").toString())
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: e.toString())
        }
        val on = ServerService.running
        status.text = if (on) "Server ON" else "Server OFF"
        status.setTextColor(if (on) 0xFF067647.toInt() else 0xFF98A2B3.toInt())
        toggle.text = if (on) "Stop server" else "Start server"
        details.text = buildString {
            append("This phone : http://localhost:${ServerService.PORT}\n")
            append("Phone B    : ${phoneBUrl()}\n")
            append("Token      : ${info.optString("token", "?")}\n")
            append("Engine     : revolver ${info.optString("version", "?")}")
            ServerService.lastError?.let { append("\n\nLast error: $it") }
            if (info.has("error")) append("\n\nPython error: ${info.optString("error")}")
        }
        keysLine.text = "Keys configured: ${info.optInt("keys", 0)}"
    }

    private fun phoneBUrl(): String {
        val ip = info.optString("ip", "")
        return if (ip.isEmpty() || ip == "null") "(connect to Wi-Fi)"
        else "http://$ip:${ServerService.PORT}"
    }

    private fun toggleServer() {
        val i = Intent(this, ServerService::class.java)
        if (ServerService.running) {
            i.action = ServerService.ACTION_STOP
            startService(i)
        } else {
            if (info.optInt("keys", 0) == 0) {
                toast("Add at least one key first")
                return
            }
            startForegroundService(i)
        }
        ui.postDelayed({ refresh() }, 700)
    }

    private fun addKeysDialog() {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 16, 48, 0)
        }
        val keys = EditText(this).apply {
            hint = "Paste Groq API keys, one per line"
            minLines = 4
            inputType = InputType.TYPE_CLASS_TEXT or
                InputType.TYPE_TEXT_FLAG_MULTI_LINE or
                InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        }
        val model = EditText(this).apply {
            setText("qwen/qwen3.8-27b")
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        }
        box.addView(keys)
        box.addView(TextView(this).apply { text = "Model" })
        box.addView(model)
        AlertDialog.Builder(this)
            .setTitle("Add Groq keys")
            .setView(box)
            .setPositiveButton("Add") { _, _ ->
                try {
                    val n = Py.call(
                        this, "add_keys", keys.text.toString(), "groq",
                        model.text.toString().trim(), 200000, 1000
                    ).toInt()
                    toast("Added $n key(s)")
                } catch (e: Exception) {
                    toast("Error: ${e.message}")
                }
                refresh()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun askBattery() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) {
            toast("Already allowed to run in background")
            return
        }
        try {
            startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))
            )
        } catch (e: ActivityNotFoundException) {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    private fun openPage(path: String) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("http://localhost:${ServerService.PORT}$path")))
        } catch (e: ActivityNotFoundException) {
            toast("No browser found")
        }
    }

    private fun copy(labelText: String, value: String) {
        val cm = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText(labelText, value))
        toast("Copied $labelText")
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}
