package com.akosidave.revolver

import android.content.Context
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File

/** Starts Python once per process and points the revolver at app storage. */
object Py {
    @Volatile private var ready = false

    @Synchronized
    fun module(ctx: Context): PyObject {
        if (!Python.isStarted()) Python.start(AndroidPlatform(ctx.applicationContext))
        val m = Python.getInstance().getModule("android_entry")
        if (!ready) {
            m.callAttr("init", File(ctx.filesDir, "revolver").absolutePath)
            ready = true
        }
        return m
    }

    fun call(ctx: Context, fn: String, vararg args: Any): PyObject =
        module(ctx).callAttr(fn, *args)
}
